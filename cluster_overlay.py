"""cluster_overlay.py — the restricted peer-only overlay one distributed rank runs inside.

What "restricted peer-only overlay" guarantees (and what verify() re-checks in the kernel before
any buyer code may use it):

  * The rank's workload runs in its OWN network namespace whose only interfaces are `lo` and one
    WireGuard device `pbwg0`, and whose only routes are this job's overlay subnet on pbwg0. No
    default route, no IPv6 route off lo, no bridge/veth. So the workload has no path to the
    seller's host, the seller's LAN, cloud metadata, the platform gateway or the internet.
  * pbwg0's peer set is EXACTLY the other ranks of this job, one peer per rank with AllowedIPs =
    that rank's /32. WireGuard's cryptokey routing drops a packet addressed to an IP no peer owns,
    and drops a received packet unless it comes from a configured key AND from that key's /32.
    Between ranks of the same job every port is open: they are the same buyer's processes, and
    NCCL/gloo use ephemeral ports, so port-pinning would break collectives without protecting
    anyone. The boundary is the job's peer set, not a port list.
  * The device is CREATED in the host namespace and then MOVED, so its UDP socket stays in the
    host namespace: only WireGuard ciphertext to the peers' endpoints ever leaves the host, and the
    plaintext side exists only inside the job's namespace. Endpoints are the public addresses the
    API itself observed; a non-global endpoint is refused, so the host never sends even ciphertext
    into a LAN.
  * Every job has its own keys, device, listen port and namespace. Nothing is shared with another
    job or with the gateway's WireGuard (wg0) and its peers.
  * The workload runs with --cap-drop ALL, so it cannot change pbwg0's peers, addresses or routes.
  * Teardown deletes the namespace (named netns) or removes the container; the device dies with it.

NAT traversal: sellers sit behind home routers, so the port a peer must send to is the router's
public mapping of the rank's WireGuard port, not the local one. create() keeps that local port
bound and asks two STUN servers what it maps to (STUN = RFC 5389 binding request, stdlib only).
The rank registers the MAPPED port. A router that maps differently per destination (symmetric
NAT) cannot be traversed this way and is refused up front. While the rank waits for the cluster,
keepalive() re-asks every STUN_REFRESH_S seconds so the mapping stays open; attach() releases the
socket immediately before WireGuard binds the same port, and both sides then send to each
other's mapping, which opens each router's filter (simultaneous open; WireGuard retries every 5 s).

Linux + root + iproute2 + wireguard-tools + nsenter. probe() says up front when they are missing.
"""
import base64
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import time

SUBNET = ipaddress.ip_network("10.77.0.0/24")   # exists only inside each rank's own namespace
IFACE = "pbwg0"
# Fallback tunnel devices the kernel puts in EVERY new netns once the ipip/sit/ip6_tunnel/gre modules
# are loaded (common on real hosts; GitHub's runners have them). Unconfigured and DOWN they are no
# path at all, and the workload has no NET_ADMIN to change that. Accepted only by exact name + kind,
# only while DOWN; an address or route on one is still flagged below.
FALLBACK_TUNNELS = {"tunl0": "ipip", "sit0": "sit", "ip6tnl0": "ip6tnl", "ip6gre0": "ip6gre", "gre0": "gre",
                    "gretap0": "gretap", "erspan0": "erspan", "ip_vti0": "vti", "ip6_vti0": "vti6"}
KEEPALIVE = 25                                  # keeps NAT mappings open between home routers
STUN_REFRESH_S = 15                             # well inside the common 30 s UDP mapping timeout


class OverlayError(RuntimeError):
    pass


_STUN_COOKIE = 0x2112A442


def stun_servers():
    """host:port list; read at call time so an operator (or a test) can point it elsewhere."""
    raw = os.getenv("DIST_STUN_SERVERS", "stun.cloudflare.com:3478,stun.l.google.com:19302")
    out = []
    for item in raw.split(","):
        host, _, port = item.strip().rpartition(":")
        if host and port.isdigit():
            out.append((host, int(port)))
    return out


def stun_parse(data, txid):
    """(ip, port) from a STUN binding success response, or None. Pure."""
    if len(data) < 20:
        return None
    mtype, mlen, cookie = struct.unpack(">HHI", data[:8])
    if mtype != 0x0101 or cookie != _STUN_COOKIE or data[8:20] != txid:
        return None
    i, end, plain = 20, min(len(data), 20 + mlen), None
    while i + 4 <= end:
        atype, alen = struct.unpack(">HH", data[i:i + 4])
        val = data[i + 4:i + 4 + alen]
        if atype in (0x0020, 0x0001) and len(val) >= 8 and val[1] == 0x01:   # (XOR-)MAPPED-ADDRESS, IPv4
            port = struct.unpack(">H", val[2:4])[0]
            addr = val[4:8]
            if atype == 0x0020:      # preferred: a router ALG cannot rewrite the XOR'd form
                port ^= _STUN_COOKIE >> 16
                addr = bytes(a ^ b for a, b in zip(addr, struct.pack(">I", _STUN_COOKIE)))
                return socket.inet_ntoa(addr), port
            plain = plain or (socket.inet_ntoa(addr), port)
        i += 4 + alen + (-alen % 4)
    return plain


def stun_mapped(sock, server, timeout=2.0):
    """Ask one STUN server what this socket's address looks like from outside. -> (ip, port)|None"""
    txid = secrets.token_bytes(12)
    try:
        addr = socket.getaddrinfo(server[0], server[1], socket.AF_INET, socket.SOCK_DGRAM)[0][4]
    except OSError:
        return None
    for _ in range(2):                                   # one retransmit: a single lost datagram
        try:                                             # must not read as "no answer"
            sock.sendto(struct.pack(">HHI", 0x0001, 0, _STUN_COOKIE) + txid, addr)
        except OSError:
            return None
        deadline = time.monotonic() + timeout
        while (left := deadline - time.monotonic()) > 0:
            sock.settimeout(left)
            try:
                data, _src = sock.recvfrom(2048)
            except socket.timeout:
                break
            except OSError:
                return None
            got = stun_parse(data, txid)
            if got:
                return got
    return None


def discover_mapping(sock):
    """The public port this socket's traffic leaves with, from up to two STUN servers.

    None when no server answered (no NAT information: the local port is used, as before).
    OverlayError when two servers see different ports: the router allocates a new port per
    destination (symmetric NAT), so the peers' packets could never reach this rank."""
    seen = []
    for server in stun_servers():
        got = stun_mapped(sock, server)
        if got:
            seen.append(got)
        if len(seen) == 2:
            break
    if not seen:
        return None
    if len({port for _ip, port in seen}) > 1:
        raise OverlayError("this host's NAT gives every destination a different port (symmetric NAT); "
                           "forward one UDP port to this machine, set DIST_WG_PORT to it and set "
                           "DIST_STUN_SERVERS empty so the forwarded port is advertised")
    return seen[0][1]


def overlay_ip(rank) -> str:
    """Rank r's address on the overlay. The API derives the same value; nothing is negotiated."""
    r = int(rank)
    if not 0 <= r < SUBNET.num_addresses - 3:
        raise OverlayError("rank outside the overlay subnet")
    return str(SUBNET.network_address + 1 + r)


def valid_key(key) -> bool:
    try:
        return isinstance(key, str) and len(key) == 44 and len(base64.b64decode(key, validate=True)) == 32
    except Exception:                                   # noqa: BLE001
        return False


def _endpoint(value):
    host, _, port = str(value or "").rpartition(":")
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
        port = int(port)
    except ValueError:
        raise OverlayError("malformed peer endpoint") from None
    if not ip.is_global:
        raise OverlayError("non-public peer endpoint refused")
    if not 0 < port < 65536:
        raise OverlayError("malformed peer endpoint")
    return f"[{ip}]:{port}" if ip.version == 6 else f"{ip}:{port}"


def parse_peers(peers, my_rank, world_size) -> list:
    """The API's rendezvous peer list -> WireGuard peers for every OTHER rank of this job.

    Refuses (OverlayError) unless there is exactly one entry per rank 0..world_size-1, every key is
    a real WireGuard key and distinct, and every other rank's endpoint is a public address."""
    seen, keys, out = set(), set(), []
    for p in peers or []:
        rank = int(p.get("rank", -1))
        key = p.get("public_key")
        if not 0 <= rank < int(world_size) or rank in seen:
            raise OverlayError("peer list has an unknown or duplicate rank")
        if not valid_key(key) or key in keys:
            raise OverlayError("peer list has an invalid or duplicate key")
        seen.add(rank)
        keys.add(key)
        if rank != int(my_rank):
            out.append({"rank": rank, "public_key": key, "endpoint": _endpoint(p.get("endpoint")),
                        "allowed_ip": overlay_ip(rank) + "/32"})
    if len(seen) != int(world_size):
        raise OverlayError("peer list is incomplete")
    return sorted(out, key=lambda p: p["rank"])


def check_namespace(links, addrs, routes4, routes6, allowed_ips, rank, peers) -> list:
    """Pure: the problems (empty = isolated) in one namespace, given the kernel's JSON views
    (`ip -j -d link`, `ip -j addr`, `ip -j -4|-6 route show table all`) and `wg show pbwg0
    allowed-ips`. Kept pure so every way isolation can break has a unit test."""
    bad = []
    inert = {l.get("ifname") for l in links
             if l.get("ifname") in FALLBACK_TUNNELS     # never None == None for a kind-less eth0
             and FALLBACK_TUNNELS[l.get("ifname")] == (l.get("linkinfo") or {}).get("info_kind")
             and "UP" not in (l.get("flags") or [])}
    names = {l.get("ifname") for l in links} - inert
    if names != {"lo", IFACE}:
        bad.append(f"unexpected interfaces {sorted(n for n in names if n)}")
    for l in links:
        if l.get("ifname") == IFACE and ((l.get("linkinfo") or {}).get("info_kind") != "wireguard"
                                         or "UP" not in (l.get("flags") or [])):
            bad.append(f"{IFACE} is not an up wireguard device")
    want = {"lo": {("inet", "127.0.0.1", 8), ("inet6", "::1", 128)},
            IFACE: {("inet", overlay_ip(rank), SUBNET.prefixlen)}}
    for a in addrs:
        got = {(i.get("family"), i.get("local"), i.get("prefixlen")) for i in a.get("addr_info") or []}
        if not got <= want.get(a.get("ifname"), set()):
            bad.append(f"unexpected address on {a.get('ifname')}")
    for r in routes4:
        dst, dev = r.get("dst", "default"), r.get("dev")
        if dst == "default":
            bad.append("an IPv4 default route exists")
            continue
        net = ipaddress.ip_network(dst, strict=False)
        if not ((dev == IFACE and net.subnet_of(SUBNET))
                or (dev == "lo" and net.subnet_of(ipaddress.ip_network("127.0.0.0/8")))):
            bad.append(f"IPv4 route {dst} via {dev}")
    for r in routes6:
        dst, dev = r.get("dst", "default"), r.get("dev")
        if dev != "lo" and not (dev == IFACE and dst == "ff00::/8"):   # multicast scope only
            bad.append(f"IPv6 route {dst} via {dev}")
    want_peers = {(p["public_key"], p["allowed_ip"]) for p in peers}
    got_peers = set()
    for line in (allowed_ips or "").splitlines():
        key, _, ips = line.partition("\t")
        for ip in ips.split() or ["(none)"]:
            got_peers.add((key, ip))
    if got_peers != want_peers:
        bad.append("wireguard peer set differs from this job's ranks")
    return bad


_V4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d{1,5})?\b")
_V6 = re.compile(r"\[?(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?:%\w+)?\]?(?::\d{1,5})?")
_PORTS = re.compile(r"(?i)\b((?:udp |tcp )?(?:listen-)?ports?\s*[:=#]?\s*)"
                     r"(\d{1,5}(?:\s*(?:,|/|-|\bto\b|\band\b|\bor\b)\s*\d{1,5})*)\b")


def _v6(m):
    core = m.group(0).lstrip("[").split("]")[0].split("%")[0]
    try:
        ipaddress.IPv6Address(core)           # only a real IPv6 address: "HTTP:500" / "12:34:56" stay
        return "<address>"
    except ValueError:
        return m.group(0)


def public_reason(text, limit=300) -> str:
    """A rank's failure reason as the BUYER may see it: every IPv4 address, every
    real IPv6 address (with any :port) and every number of a stated port list is replaced,
    so no seller, peer or LAN endpoint leaves the agent; the text is capped."""
    flat = _V6.sub(_v6, _V4.sub("<address>", " ".join(str(text or "").split())))
    flat = _PORTS.sub(lambda m: m.group(1) + re.sub(r"\d+", "<port>", m.group(2)), flat)
    return flat[:limit]


def unconnected(latest_handshakes, peers) -> list:
    """Pure: the peers (by rank) with no completed WireGuard handshake yet, given
    `wg show pbwg0 latest-handshakes` ("<pubkey>\t<unix time, 0 = never>" per line)."""
    seen = {}
    for line in (latest_handshakes or "").splitlines():
        key, _, ts = line.partition("\t")
        seen[key.strip()] = ts.strip()
    return [p["rank"] for p in peers if seen.get(p["public_key"], "0") in ("", "0")]


def probe():
    """(ok, reason): can this host build the overlay at all? Creates nothing."""
    if sys.platform != "linux" or os.geteuid() != 0:
        return False, "the cluster overlay needs the agent running as root on Linux"
    missing = [t for t in ("ip", "wg", "nsenter") if not shutil.which(t)]
    if missing:
        return False, "missing " + ", ".join(missing) + " (apt-get install iproute2 wireguard-tools util-linux)"
    return True, None


def _run(args, *, stdin=None):
    r = subprocess.run(args, input=stdin, capture_output=True, text=True, timeout=20)
    if r.returncode:
        # Name the command only; never echo stderr/stdin (a private key may be on stdin).
        raise OverlayError(f"cluster overlay: `{' '.join(args[:5])}` failed")
    return r.stdout


class RankOverlay:
    """One rank's overlay device: create (host ns) -> set_peers -> attach (job ns) -> verify."""

    def __init__(self, task_id):
        tid = int(task_id)
        self.dev = f"pbc{tid}"
        if tid <= 0 or len(self.dev) > 15:
            raise OverlayError("invalid task id for the overlay device")
        self.netns = None
        self.exec = None
        self._tid = tid
        self._hold = None          # the WireGuard port, held open for STUN until attach()
        self._last_stun = 0.0

    def create(self, port=0):
        """Create the device in the HOST namespace (its UDP socket stays there).
        -> (pubkey, public_port): the port peers must send to (the NAT mapping when there is one).

        WireGuard only binds its port when the device goes up (after the move), so the port is
        chosen and held here; attach() confirms the kernel bound exactly it."""
        hold = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            hold.bind(("0.0.0.0", int(port)))
        except OSError:
            hold.close()
            raise OverlayError("cluster overlay: the WireGuard port is already in use") from None
        self._hold, self.port = hold, hold.getsockname()[1]
        self.public_port = discover_mapping(hold) or self.port
        self._last_stun = time.monotonic()
        _run(["ip", "link", "add", self.dev, "type", "wireguard"])
        priv = _run(["wg", "genkey"]).strip()
        pub = _run(["wg", "pubkey"], stdin=priv).strip()
        _run(["wg", "set", self.dev, "private-key", "/dev/stdin", "listen-port", str(self.port)], stdin=priv)
        return pub, self.public_port

    def keepalive(self):
        """While waiting for the other ranks: keep the NAT mapping open, and refuse if it moved."""
        if self._hold is None or time.monotonic() - self._last_stun < STUN_REFRESH_S:
            return
        self._last_stun = time.monotonic()
        server = (stun_servers() or [None])[0]
        got = stun_mapped(self._hold, server) if server else None
        if got and got[1] != self.public_port:
            raise OverlayError("this host's NAT mapping changed while the cluster formed")

    def _release(self):
        if self._hold is not None:
            self._hold.close()
            self._hold = None

    def set_peers(self, peers):
        for p in peers:
            _run(["wg", "set", self.dev, "peer", p["public_key"], "allowed-ips", p["allowed_ip"],
                  "endpoint", p["endpoint"], "persistent-keepalive", str(KEEPALIVE)])

    def attach(self, rank, pid=None):
        """Move the device into the workload's namespace — a container's (pid) or a fresh named
        one this object owns — and bring up only lo + pbwg0 there."""
        if pid is None:
            self.netns = f"pbc-ns-{self._tid}"
            _run(["ip", "netns", "add", self.netns])
            target, self.exec = self.netns, ["ip", "netns", "exec", self.netns]
        else:
            target, self.exec = str(int(pid)), ["nsenter", "-t", str(int(pid)), "-n"]
        _run(["ip", "link", "set", self.dev, "netns", target])
        x = self.exec
        _run(x + ["ip", "link", "set", self.dev, "name", IFACE])
        _run(x + ["ip", "addr", "add", f"{overlay_ip(rank)}/{SUBNET.prefixlen}", "dev", IFACE])
        _run(x + ["ip", "link", "set", "lo", "up"])
        self._release()                # WireGuard binds this port on up; the NAT mapping carries over
        _run(x + ["ip", "link", "set", IFACE, "up"])
        if _run(x + ["wg", "show", IFACE, "listen-port"]).strip() != str(self.port):
            raise OverlayError("cluster overlay: the registered WireGuard port is not the bound one")

    def problems(self, rank, peers) -> list:
        x = self.exec
        j = lambda *a: json.loads(_run(x + ["ip", "-j", *a]) or "[]")   # noqa: E731
        return check_namespace(j("-d", "link", "show"), j("addr", "show"),
                               j("-4", "route", "show", "table", "all"),
                               j("-6", "route", "show", "table", "all"),
                               _run(x + ["wg", "show", IFACE, "allowed-ips"]), rank, peers)

    def wait_connected(self, peers, timeout=None):
        """Every peer must complete a WireGuard handshake (persistent keepalive starts one the moment
        pbwg0 is up) within `timeout`, else the rank refuses with a clear cause -- instead of the
        workload waiting out NCCL/rendezvous timeouts against a mesh that never formed."""
        timeout = float(timeout if timeout is not None else os.getenv("DIST_CONNECT_TIMEOUT", "120"))
        deadline = time.monotonic() + timeout
        while True:
            missing = unconnected(_run(self.exec + ["wg", "show", IFACE, "latest-handshakes"]), peers)
            if not missing:
                return
            if time.monotonic() >= deadline:
                raise OverlayError(
                    f"could not reach rank(s) {missing} over the overlay within {int(timeout)}s: a NAT or "
                    "firewall blocks UDP between the machines (behind a symmetric NAT, forward a UDP port, "
                    "set DIST_WG_PORT to it and DIST_STUN_SERVERS empty)")
            time.sleep(1)

    def verify(self, rank, peers):
        bad = self.problems(rank, peers)
        if bad:
            raise OverlayError("cluster overlay isolation check failed: " + "; ".join(bad)[:300])

    def teardown(self):
        """Best effort and idempotent. A moved device dies with its namespace."""
        self._release()
        for args in (["ip", "link", "del", self.dev],
                     ["ip", "netns", "del", self.netns] if self.netns else None):
            if args:
                subprocess.run(args, capture_output=True, timeout=20)
