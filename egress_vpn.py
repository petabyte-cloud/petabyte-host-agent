"""Force a buyer container's INTERNET egress through the Petabyte gateway, as a WireGuard VPN.

Without this, a rented container's outbound traffic NATs straight out the SELLER's NIC, so any
service the buyer contacts — or `curl ifconfig.me` inside the VM — sees the seller's public IP, and
the buyer rides the seller's IP reputation. With this enabled, each per-job bridge's internet-bound
traffic is policy-routed into a `wg-egress` tunnel to the gateway, which NATs it out the GATEWAY's
IP. The seller's IP never reaches the internet on a buyer's behalf.

Design (proven end-to-end):
  * wg-egress is a CLIENT tunnel to the gateway, brought up with `Table = off` so it NEVER hijacks
    the node's own default route — the agent<->API control plane, heartbeats and the reverse tunnel
    keep using the normal NIC. Only buyer bridges are policy-routed into it.
  * per bridge: local/private destinations stay in the main table (so the inbound reverse-tunnel
    publish + docker-proxy return path keep working); everything else (the internet) goes to a
    dedicated route table whose default is `wg-egress`.
  * SNAT the bridge into the tunnel (the gateway masquerades the tunnel subnet to the internet).
  * MSS-clamp both directions (the tunnel MTU is < 1500, so unclamped TLS blackholes on PMTUD).
  * FAIL-CLOSED: a DROP for bridge->WAN means that if the tunnel is down the container gets NO
    internet — it can never silently fall back to the seller's NIC.

Config (env, typically /etc/petabyte/agent.env — the API assigns the address + adds this node as a
peer on the gateway, exactly like the buyer-WireGuard peer push):
  PB_EGRESS_GATEWAY_PUBKEY    the gateway wg-egress public key
  PB_EGRESS_GATEWAY_ENDPOINT  host:port of the gateway wg-egress listener
  PB_EGRESS_ADDR              this node's tunnel address, e.g. 10.9.0.7/32
  PB_EGRESS_MTU               tunnel MTU (default 1420)  -> clamp MSS = MTU-40
  PB_EGRESS_DIRECT_HOSTS      comma-separated in-country object-storage hostnames (*.aliyuncs.com)
                              reached DIRECTLY, not via the tunnel (normally pushed by the API in
                              the heartbeat reply from EGRESS_DIRECT_STORAGE_HOSTS). Empty = off.
Absent config -> disabled (a no-op), so existing nodes are unaffected until enrolled.
"""
from __future__ import annotations

import ipaddress
import os
import re
import shlex
import shutil
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait

IFACE = "wg-egress"
TABLE = "51821"                       # dedicated route table id for buyer internet egress
WG_DIR = os.getenv("AGENT_WG_DIR", "/etc/wireguard")
NODE_KEY = os.path.join(WG_DIR, "egress_node_private.key")
# Destinations that must NOT be tunneled: loopback, the RFC1918/CGNAT/link-local ranges (the
# docker-proxy return path, the bridge itself, cloud metadata, LAN). Internet == everything else.
LOCAL_NETS = ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
              "169.254.0.0/16", "100.64.0.0/10")


def enabled() -> bool:
    return bool(os.getenv("PB_EGRESS_GATEWAY_PUBKEY") and os.getenv("PB_EGRESS_GATEWAY_ENDPOINT")
                and os.getenv("PB_EGRESS_ADDR"))


def _mtu() -> int:
    try:
        return max(1280, int(os.getenv("PB_EGRESS_MTU", "1420")))
    except (TypeError, ValueError):
        return 1420


def _mss() -> int:
    return _mtu() - 40


def _run(args, check=True):
    r = subprocess.run(args, capture_output=True, text=True, timeout=20)
    if check and r.returncode:
        raise RuntimeError(f"{' '.join(args)}: {r.stderr.strip()}")
    return r


def node_pubkey() -> str:
    """This node's wg-egress public key (generating the keypair once). The API sends it to the
    gateway to authorize this node as an egress peer."""
    os.makedirs(WG_DIR, exist_ok=True)
    if not os.path.exists(NODE_KEY):
        priv = _run(["wg", "genkey"]).stdout.strip()
        old = os.umask(0o077)
        try:
            with open(NODE_KEY, "w") as f:
                f.write(priv)
        finally:
            os.umask(old)
    priv = open(NODE_KEY).read().strip()
    return subprocess.run(["wg", "pubkey"], input=priv, capture_output=True, text=True).stdout.strip()


def _iface_up() -> bool:
    return subprocess.run(["ip", "link", "show", IFACE], capture_output=True).returncode == 0


def ensure_tunnel() -> bool:
    """Bring the wg-egress CLIENT up (idempotent). Returns True when the interface is up. Never
    touches the node's default route (Table = off). rp_filter is set loose so tunnel return packets
    (whose source is reachable only via the normal NIC) are not reverse-path-dropped."""
    if not enabled() or not (shutil.which("wg") and shutil.which("wg-quick")):
        return False
    node_pubkey()                                        # ensure keypair exists
    # The conf goes under /run, NOT WG_DIR: the agent unit's ProtectSystem=full makes /etc
    # read-only (EROFS), which failed every per-job network. The key stays in WG_DIR — install.sh
    # (plain root) creates it; here it is only read.
    os.makedirs(STATE_DIR, exist_ok=True)
    conf = os.path.join(STATE_DIR, IFACE + ".conf")
    priv = open(NODE_KEY).read().strip()
    body = (
        "[Interface]\n"
        f"Address = {os.getenv('PB_EGRESS_ADDR')}\n"
        f"PrivateKey = {priv}\n"
        f"MTU = {_mtu()}\n"
        "Table = off\n"
        "[Peer]\n"
        f"PublicKey = {os.getenv('PB_EGRESS_GATEWAY_PUBKEY')}\n"
        f"Endpoint = {os.getenv('PB_EGRESS_GATEWAY_ENDPOINT')}\n"
        "AllowedIPs = 0.0.0.0/0\n"
        "PersistentKeepalive = 25\n"
    )
    old = os.umask(0o077)
    try:
        with open(conf, "w") as f:
            f.write(body)
    finally:
        os.umask(old)
    if not _iface_up():
        _run(["wg-quick", "up", conf], check=False)
    # Game UDP replies arrive from the gateway's 10.9.0.1 address. Keep this host-only /32 route
    # on the WireGuard interface; Table=off deliberately avoids changing the seller's default route.
    _run(["ip", "route", "replace", "10.9.0.1/32", "dev", IFACE], check=False)
    for knob in ("all", "default", IFACE):
        subprocess.run(["sysctl", "-w", f"net.ipv4.conf.{knob}.rp_filter=2"],
                       capture_output=True)
    return _iface_up()


def service_udp_rule(port):
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("invalid native UDP listener")
    address = str(ipaddress.ip_interface(os.environ["PB_EGRESS_ADDR"]).ip)
    return ["-i", IFACE, "-s", "10.9.0.1/32", "-d", address + "/32", "-p", "udp",
            "--dport", str(port), "-m", "comment", "--comment", "pb-native-service", "-j", "ACCEPT"]


def peer_ready():
    """A configured/up interface alone does not prove the gateway is reachable."""
    if not enabled() or not shutil.which("wg"):
        return False
    result = _run(["wg", "show", IFACE, "latest-handshakes"], check=False)
    if result.returncode:
        return False
    for row in result.stdout.splitlines():
        fields = row.split()
        if len(fields) == 2 and fields[0] == os.getenv("PB_EGRESS_GATEWAY_PUBKEY"):
            try:
                return 0 <= time.time() - int(fields[1]) <= 180
            except ValueError:
                return False
    return False


def service_udp_firewall(port, remove=False):
    """Open only this authenticated listener to the gateway on WireGuard, never WAN."""
    rule = service_udp_rule(port)
    present = _run(["iptables", "-C", "INPUT", *rule], check=False).returncode == 0
    if present and remove:
        _run(["iptables", "-D", "INPUT", *rule])
    elif not present and not remove:
        _run(["iptables", "-I", "INPUT", "1", *rule])


def bridge_commands(subnet: str, wan: str, *, table: str = TABLE, mss: int | None = None):
    """The exact ip/iptables commands that route ONE bridge subnet's internet egress through the
    tunnel, fail-closed. Pure (builds the list; runs nothing) so it is unit-testable."""
    ipaddress.ip_network(subnet, strict=False)           # validate / reject junk
    mss = mss or _mss()
    cmds = [
        # dedicated table: default via the tunnel
        ["ip", "route", "replace", "default", "dev", IFACE, "table", table],
        # local/private dsts stay local (inbound publish + docker-proxy return path)
        *[["ip", "rule", "add", "from", subnet, "to", net, "lookup", "main", "priority", "90"]
          for net in (subnet,) + LOCAL_NETS],
        # everything else (the internet) -> the tunnel table
        ["ip", "rule", "add", "from", subnet, "lookup", table, "priority", "100"],
        # SNAT the bridge into the tunnel (gateway masquerades the tunnel subnet to the internet)
        ["iptables", "-t", "nat", "-A", "POSTROUTING", "-s", subnet, "-o", IFACE, "-j", "MASQUERADE"],
        # MSS clamp BOTH directions (tunnel MTU < 1500)
        ["iptables", "-t", "mangle", "-A", "FORWARD", "-o", IFACE,
         "-p", "tcp", "--tcp-flags", "SYN,RST", "SYN", "-j", "TCPMSS", "--set-mss", str(mss)],
        ["iptables", "-t", "mangle", "-A", "FORWARD", "-i", IFACE,
         "-p", "tcp", "--tcp-flags", "SYN,RST", "SYN", "-j", "TCPMSS", "--set-mss", str(mss)],
        # FAIL-CLOSED: this bridge may NEVER egress directly out the seller NIC
        ["iptables", "-I", "DOCKER-USER", "1", "-s", subnet, "-o", wan, "-j", "DROP"],
    ]
    return cmds


# ---- direct in-country object storage (bypasses the tunnel) -------------------------------------
# Client audio/data to the platform's in-country bucket (Saudi: Alibaba OSS me-central-1) should
# not be relayed (and billed) through the gateway. Only the exact resolved /32s of allowlisted
# storage hostnames bypass; everything else stays tunnelled and fail-closed.
DIRECT_ENV = "PB_EGRESS_DIRECT_HOSTS"
DIRECT_COMMENT = "pb-egress-direct"
MAX_DIRECT_HOSTS, MAX_DIRECT_IPS = 8, 32
DIRECT_REFRESH_S = 300
# Enforced HERE (not only on the API): a bad push can never open a bypass to an arbitrary host.
_DIRECT_HOST_RE = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+aliyuncs\.com")
_LOCK = threading.RLock()            # heartbeat refresh vs job-start route_bridge (xtables lock)
_direct_at = 0.0
# One shared, fixed-size resolver pool + at most one lookup in flight per host: a hung getaddrinfo
# can't be cancelled, so a timed-out one is re-awaited by the next refresh, never re-submitted.
_DNS_POOL = ThreadPoolExecutor(max_workers=MAX_DIRECT_HOSTS, thread_name_prefix="pb-egress-dns")
_DNS_LOCK = threading.Lock()
_dns_inflight: dict = {}


def direct_hosts(raw: str | None = None) -> list[str]:
    """The validated direct-storage hostnames (bad entries dropped, capped)."""
    hosts = []
    for h in (os.getenv(DIRECT_ENV, "") if raw is None else raw).split(","):
        h = h.strip().lower().rstrip(".")
        if len(h) <= 253 and _DIRECT_HOST_RE.fullmatch(h) and h not in hosts:
            hosts.append(h)
    return hosts[:MAX_DIRECT_HOSTS]


def _public_v4(ip: str) -> bool:
    """A DNS answer may only become a bypass if it is a plain public unicast IPv4 address: never
    private/loopback/link-local (169.254.169.254 metadata)/CGNAT/multicast/reserved."""
    try:
        a = ipaddress.IPv4Address(ip)
    except ValueError:
        return False
    return (a.is_global and not (a.is_multicast or a.is_reserved)
            and not any(a in ipaddress.ip_network(n) for n in LOCAL_NETS))


def direct_ips(hosts: list[str] | None = None, timeout: float = 5.0) -> list[str]:
    """A records of the direct hosts (public IPv4 only, capped). DNS failure = no bypass."""
    hosts = direct_hosts() if hosts is None else hosts
    if not hosts:
        return []
    with _DNS_LOCK:
        for h in hosts:
            if h not in _dns_inflight or _dns_inflight[h].done():
                _dns_inflight[h] = _DNS_POOL.submit(socket.getaddrinfo, h, 443, socket.AF_INET,
                                                    socket.SOCK_STREAM)
        futs = [_dns_inflight[h] for h in hosts]
    done, _ = wait(futs, timeout=timeout)                 # a hung resolver never stalls a launch
    ips = []
    for f in futs:
        if f in done and f.exception() is None:
            for info in f.result():
                ip = info[4][0]
                if _public_v4(ip) and ip not in ips:
                    ips.append(ip)
    return ips[:MAX_DIRECT_IPS]


def direct_commands(subnet: str, wan: str, ips) -> list[list[str]]:
    """Per storage IP: ACCEPT bridge->IP out the WAN (inserted above the fail-closed DROP), then
    keep that exact /32 in the main table. Docker's own per-bridge MASQUERADE NATs it out the NIC.
    Pure, like bridge_commands. ACCEPT first so a half-applied pair never blackholes."""
    ipaddress.ip_network(subnet, strict=False)
    cmds = []
    for ip in ips:
        if not _public_v4(ip):
            raise ValueError("direct storage address must be public IPv4")
        cmds += [["iptables", "-I", "DOCKER-USER", "1", "-s", subnet, "-d", ip + "/32", "-o", wan,
                  "-m", "comment", "--comment", DIRECT_COMMENT, "-j", "ACCEPT"],
                 ["ip", "rule", "add", "from", subnet, "to", ip, "lookup", "main", "priority", "90"]]
    return cmds


def _direct_rules(subnet: str) -> dict:
    """{ip: iptables -S rule} of this bridge's current direct-storage ACCEPTs. Raises when the chain
    can't be listed: a failed listing must never read as "no bypass installed"."""
    out = _run(["iptables", "-S", "DOCKER-USER"]).stdout or ""
    found = {}
    for line in out.splitlines():
        a = shlex.split(line)
        if ("--comment" in a and a[a.index("--comment") + 1] == DIRECT_COMMENT
                and "-s" in a and a[a.index("-s") + 1] == subnet and "-d" in a):
            found[a[a.index("-d") + 1].split("/")[0]] = a
    return found


def sync_direct(subnet: str, wan: str | None = None, ips=None) -> None:
    """Make this bridge's direct-storage bypass match `ips` (default: resolve now). Stale IPs are
    removed ACCEPT-first, so their traffic falls back to the tunnel, never to an open WAN. Raises if
    the bypass rules can't be listed or a stale ACCEPT can't be removed."""
    with _LOCK:
        ips = direct_ips() if ips is None else ips
        for ip, rule in _direct_rules(subnet).items():
            if ip not in ips:
                _run(["iptables", "-D", *rule[1:]])
                subprocess.run(["ip", "rule", "del", "from", subnet, "to", ip, "lookup", "main",
                                "priority", "90"], capture_output=True)
        wan = wan or _wan_iface()
        if ips and wan:
            _apply(direct_commands(subnet, wan, ips))


def _recorded_subnets() -> list[str]:
    try:
        names = [n for n in os.listdir(STATE_DIR) if n[:1] == "t" and n[1:].isdigit()]
        return [s for s in (open(os.path.join(STATE_DIR, n)).read().strip() for n in names) if s]
    except OSError:
        return []


def note_direct_hosts(raw) -> None:
    """Heartbeat hook: adopt the API's host list and re-sync live bridges when it changed, or at
    most every DIRECT_REFRESH_S (storage A records rotate). A failed sync raises and is retried on
    the next heartbeat."""
    global _direct_at
    if not isinstance(raw, str):
        return
    if raw != os.getenv(DIRECT_ENV, ""):
        os.environ[DIRECT_ENV] = raw
        _direct_at = 0.0                                 # changed: re-sync now
    if not enabled() or time.time() - _direct_at <= DIRECT_REFRESH_S:
        return
    ips = direct_ips() if _recorded_subnets() else []    # resolve outside the lock
    with _LOCK:                                          # re-list: a bridge may have gone meanwhile
        for sub in _recorded_subnets():
            sync_direct(sub, ips=ips)
    _direct_at = time.time()                             # only once every live bridge is in sync


def route_bridge(subnet: str, wan: str | None = None) -> None:
    """Apply bridge_commands idempotently (each rule checked before add), then sync the
    direct-storage bypass. With PB_EGRESS_DIRECT_HOSTS empty that adds nothing (no DNS) but still
    removes any bypass left on this subnet, e.g. by an agent that restarted without cleaning up."""
    wan = wan or _wan_iface()
    if not wan:
        raise RuntimeError("no WAN interface for egress fail-closed rule")
    with _LOCK:
        _apply(bridge_commands(subnet, wan))
        sync_direct(subnet, wan)


def _apply(cmds) -> None:
    for cmd in cmds:
        # idempotency: skip if an equivalent rule already exists
        if cmd[0] == "iptables":
            probe = list(cmd)
            probe[probe.index("-A") if "-A" in probe else probe.index("-I")] = "-C"
            if "-I" in cmd:                                 # -I <chain> 1 ... -> -C <chain> ...
                probe = [c for c in probe if c != "1"]
            if subprocess.run(probe, capture_output=True).returncode == 0:
                continue
        elif cmd[:3] == ["ip", "rule", "add"]:
            existing = subprocess.run(["ip", "rule"], capture_output=True, text=True).stdout
            frag = " ".join(cmd[3:cmd.index("priority")])
            if frag and frag in existing:
                continue
        subprocess.run(cmd, capture_output=True)


def unroute_bridge(subnet: str, wan: str | None = None) -> None:
    """Remove this bridge's egress rules (best-effort; leaves the shared tunnel up)."""
    wan = wan or _wan_iface()
    try:
        stale = _direct_rules(subnet).values()
    except Exception:                                        # noqa: BLE001 - the rest must still go
        stale = ()
    for rule in stale:                                             # direct-storage ACCEPTs
        subprocess.run(["iptables", "-D", *rule[1:]], capture_output=True)
    while subprocess.run(["ip", "rule", "del", "from", subnet],
                         capture_output=True).returncode == 0:
        pass
    subprocess.run(["iptables", "-t", "nat", "-D", "POSTROUTING", "-s", subnet,
                    "-o", IFACE, "-j", "MASQUERADE"], capture_output=True)
    for d in ("-o", "-i"):
        subprocess.run(["iptables", "-t", "mangle", "-D", "FORWARD", d, IFACE, "-p", "tcp",
                        "--tcp-flags", "SYN,RST", "SYN", "-j", "TCPMSS", "--set-mss", str(_mss())],
                       capture_output=True)
    if wan:
        subprocess.run(["iptables", "-D", "DOCKER-USER", "-s", subnet, "-o", wan, "-j", "DROP"],
                       capture_output=True)


def _wan_iface() -> str:
    try:
        out = _run(["ip", "route", "show", "default"]).stdout
        parts = out.split()
        return parts[parts.index("dev") + 1] if "dev" in parts else ""
    except Exception:                                        # noqa: BLE001
        return ""


# The docker network (and thus its subnet) is torn down BEFORE network_policy.cleanup runs, so we
# remember each task's bridge subnet to be able to remove its egress rules afterwards.
STATE_DIR = os.getenv("PB_EGRESS_STATE_DIR", "/run/pb-egress")


def _state_path(tid) -> str:
    return os.path.join(STATE_DIR, "t" + str(int(tid)))


def record(tid, subnet) -> None:
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(_state_path(tid), "w") as f:
            f.write(subnet)
    except Exception:                                        # noqa: BLE001
        pass


def unroute_for_tid(tid) -> None:
    try:
        p = _state_path(tid)
        with _LOCK:                          # a heartbeat re-sync must not re-add rules mid-teardown
            if os.path.exists(p):
                sub = open(p).read().strip()
                if sub:
                    unroute_bridge(sub)
                os.remove(p)
    except Exception:                                        # noqa: BLE001
        pass


# ---- multi-gateway: which gateway buyer egress exits through ------------------------------------
# The API moves a node's egress to the gateway in its own country (lumaris_api/gateways.py
# egress_home) via the heartbeat reply. /etc is read-only to the agent unit, so the choice is kept
# in its StateDirectory and re-applied at start-up, before the first job network is built.
GATEWAY_OVERRIDE = os.getenv("PB_EGRESS_GATEWAY_STATE", "/var/lib/petabyte-agent/egress_gateway.json")
_WG_KEY_RE = r"^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw480]=$"
_ENDPOINT_RE = r"^[A-Za-z0-9.-]{1,253}:[0-9]{1,5}$"


def _valid_gateway(pubkey, endpoint) -> bool:
    import re
    return (isinstance(pubkey, str) and isinstance(endpoint, str)
            and re.match(_WG_KEY_RE, pubkey) is not None and re.match(_ENDPOINT_RE, endpoint) is not None
            and 0 < int(endpoint.rsplit(":", 1)[1]) < 65536)


def load_gateway_override() -> None:
    """Apply a previously chosen egress gateway to this process's env (no-op without one)."""
    import json
    try:
        with open(GATEWAY_OVERRIDE) as f:
            v = json.load(f)
    except (OSError, ValueError):
        return
    if isinstance(v, dict) and _valid_gateway(v.get("pubkey"), v.get("endpoint")) and os.getenv("PB_EGRESS_ADDR"):
        os.environ["PB_EGRESS_GATEWAY_PUBKEY"] = v["pubkey"]
        os.environ["PB_EGRESS_GATEWAY_ENDPOINT"] = v["endpoint"]


def switch_gateway(pubkey: str, endpoint: str) -> bool:
    """Re-point wg-egress at another gateway, keeping the interface (and so every bridge's policy
    route and fail-closed rule) in place: swap the one peer. Returns True when switched."""
    import json
    if not _valid_gateway(pubkey, endpoint) or not os.getenv("PB_EGRESS_ADDR"):
        return False
    old = os.getenv("PB_EGRESS_GATEWAY_PUBKEY")
    os.environ["PB_EGRESS_GATEWAY_PUBKEY"] = pubkey
    os.environ["PB_EGRESS_GATEWAY_ENDPOINT"] = endpoint
    try:
        os.makedirs(os.path.dirname(GATEWAY_OVERRIDE), exist_ok=True)
        tmp = GATEWAY_OVERRIDE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"pubkey": pubkey, "endpoint": endpoint}, f)
        os.replace(tmp, GATEWAY_OVERRIDE)
    except OSError:
        pass                                   # still switched for this run; re-sent each heartbeat
    if _iface_up() and shutil.which("wg"):
        if old and old != pubkey:
            _run(["wg", "set", IFACE, "peer", old, "remove"], check=False)
        _run(["wg", "set", IFACE, "peer", pubkey, "endpoint", endpoint, "allowed-ips", "0.0.0.0/0",
              "persistent-keepalive", "25"])
    ensure_tunnel()                            # rewrite the conf (and bring it up if it was down)
    return True
