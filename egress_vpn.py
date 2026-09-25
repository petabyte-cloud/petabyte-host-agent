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
Absent config -> disabled (a no-op), so existing nodes are unaffected until enrolled.
"""
from __future__ import annotations

import ipaddress
import os
import shutil
import subprocess

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
    for knob in ("all", "default", IFACE):
        subprocess.run(["sysctl", "-w", f"net.ipv4.conf.{knob}.rp_filter=2"],
                       capture_output=True)
    return _iface_up()


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


def route_bridge(subnet: str, wan: str | None = None) -> None:
    """Apply bridge_commands idempotently (each rule checked before add)."""
    wan = wan or _wan_iface()
    if not wan:
        raise RuntimeError("no WAN interface for egress fail-closed rule")
    for cmd in bridge_commands(subnet, wan):
        if cmd[:2] == ["ip", "rule"]:
            check = ["ip", "rule"] + (["del"] if False else [])  # ip rule has no -C; dedup below
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
        if os.path.exists(p):
            sub = open(p).read().strip()
            if sub:
                unroute_bridge(sub)
            os.remove(p)
    except Exception:                                        # noqa: BLE001
        pass
