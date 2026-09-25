"""wireguard.py — WireGuard for the Petabyte agent.

A VPN-enabled job means the buyer wants their traffic to this node (and, for a distributed
cluster, the traffic BETWEEN ranks) to ride an encrypted WireGuard tunnel on a private
10.x network instead of the public gateway. This module gives the agent:

  * pure, testable helpers — key generation, interface-config rendering, and the decision of
    whether a given job payload is VPN-enabled; and
  * a gated bring-up/tear-down (`wg-quick up/down`) that runs ONLY when the operator opted in
    (`AGENT_VPN_ENABLED=true`) and `wg` is actually installed — otherwise it no-ops safely,
    exactly like the API's WG_APPLY guard. No import here needs the rest of the agent, so it is
    unit-testable on a box without WireGuard.

Contract with the platform: the agent asks the API for its cluster peers (each rank's VPN
address) via /jobs/{job_id}/cluster, renders a wg config, and brings up the interface so the
container can reach the other ranks over WireGuard. Rank 0 also publishes its VPN address via
/jobs/rendezvous. See wireguard_startup.md for the server side.
"""
import base64
import os
import shutil
import subprocess
import tempfile

WG_DIR = os.getenv("AGENT_WG_DIR", "/etc/wireguard")


def wg_available() -> bool:
    """True only if the WireGuard userspace tools are installed."""
    return bool(shutil.which("wg") and shutil.which("wg-quick"))


def vpn_enabled() -> bool:
    """The operator must opt a node into VPN — off by default (fail-closed)."""
    return os.getenv("AGENT_VPN_ENABLED", "false").lower() == "true"


def link_is_up(name: str) -> bool:
    """True only when interface `name` exists AND carries the kernel's IFF_UP flag.

    Neither of the obvious signals works here. `ip link show <name>` exits 0 for an interface that
    exists but is administratively DOWN, and a WireGuard device reports `state UNKNOWN` even when
    it is fully operational — so the exit code and the state column both say "fine" for a dead
    tunnel. The flag list inside `<...>` is the one honest signal, and `UP` must be matched as a
    whole token so `LOWER_UP` cannot stand in for it."""
    if not shutil.which("ip"):
        return False                       # cannot prove the link is up -> fail closed
    try:
        r = subprocess.run(["ip", "-o", "link", "show", name],
                           capture_output=True, text=True, timeout=5)
    except Exception:
        return False
    if r.returncode != 0:
        return False
    flags = (r.stdout or "").split("<", 1)[-1].split(">", 1)[0]
    return "UP" in [f.strip() for f in flags.split(",")]


def interface_up(name: str = "wg0") -> bool:
    """True only if a WireGuard interface `name` EXISTS, is really WireGuard, and is UP.

    SECURITY: the cluster egress mode shares the host network namespace (`--network host`) on the
    assumption that a WireGuard mesh confines the rank to its peers. That mesh is not wired yet
    (the server does not distribute peer pubkeys), so `AGENT_VPN_ENABLED=true` alone would grant a
    buyer image the host's LAN, cloud-metadata endpoint and every co-tenant's loopback service in
    the clear. Gate on the interface truly being present, so a cluster rank is refused (and the
    buyer refunded by the server's gang semantics) until a real mesh brings the interface up —
    rather than silently exposing the host.

    Existence alone is NOT enough: `wg show wg0` exits 0 for a configured-but-DOWN interface, so a
    leftover wg0 from a torn-down mesh (or a `wg setconf` that was never `ip link set up`) would
    hand the container `--network host` with no tunnel carrying a single packet. Require the link
    to be administratively UP as well. When wireguard-tools is absent we cannot ask `wg`, so we ask
    the kernel for the device's link type instead (`ip -d link show`) and accept it only if it
    really is a wireguard device — an unrelated dummy/bridge named wg0 must not open this gate."""
    if not link_is_up(name):
        return False
    try:
        if shutil.which("wg"):
            r = subprocess.run(["wg", "show", name], capture_output=True, timeout=5)
            return r.returncode == 0
        r = subprocess.run(["ip", "-d", "link", "show", name],
                           capture_output=True, text=True, timeout=5)
        return r.returncode == 0 and "wireguard" in (r.stdout or "").lower()
    except Exception:
        return False


def is_vpn_job(job: dict) -> bool:
    """Does this job want a WireGuard tunnel? True when the buyer chose VPN, or when the job is
    one rank of a distributed cluster (whose ranks talk over the WireGuard mesh)."""
    if not isinstance(job, dict):
        return False
    if job.get("vpn") is True:
        return True
    if job.get("egress") == "cluster":
        return True
    return bool(job.get("distributed"))


def gen_keypair() -> tuple[str, str]:
    """Return (private_b64, public_b64). Uses `wg` when present; otherwise a pure-Python
    Curve25519 fallback (so tests and no-WG hosts still get a valid keypair)."""
    if shutil.which("wg"):
        try:
            priv = subprocess.run(["wg", "genkey"], check=True, capture_output=True,
                                  text=True).stdout.strip()
            pub = subprocess.run(["wg", "pubkey"], input=priv, check=True, capture_output=True,
                                 text=True).stdout.strip()
            return priv, pub
        except Exception:
            pass
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    k = X25519PrivateKey.generate()
    priv_b = k.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                             serialization.NoEncryption())
    pub_b = k.public_key().public_bytes(serialization.Encoding.Raw,
                                        serialization.PublicFormat.Raw)
    return base64.b64encode(priv_b).decode(), base64.b64encode(pub_b).decode()


def render_interface(private_key: str, address: str, *, listen_port: int = None,
                     peers: list = None, dns: str = None) -> str:
    """Render a WireGuard interface config (pure). `peers` is a list of dicts with keys
    public_key, allowed_ips, and optional endpoint/persistent_keepalive."""
    lines = ["[Interface]", f"PrivateKey = {private_key}", f"Address = {address}"]
    if listen_port:
        lines.append(f"ListenPort = {int(listen_port)}")
    if dns:
        lines.append(f"DNS = {dns}")
    for p in (peers or []):
        lines += ["", "[Peer]", f"PublicKey = {p['public_key']}",
                  f"AllowedIPs = {p.get('allowed_ips', '10.0.0.0/24')}"]
        if p.get("endpoint"):
            lines.append(f"Endpoint = {p['endpoint']}")
        if p.get("persistent_keepalive"):
            lines.append(f"PersistentKeepalive = {int(p['persistent_keepalive'])}")
    return "\n".join(lines) + "\n"


def peers_from_cluster(cluster: dict, my_rank: int) -> list:
    """Turn a /jobs/{id}/cluster response into WireGuard [Peer] entries — every OTHER rank in
    the cluster becomes a peer at its VPN address, so this node can all-reduce with them."""
    peers = []
    for node in (cluster or {}).get("nodes", []):
        if node.get("rank") == my_rank or not node.get("host"):
            continue
        peers.append({"public_key": node.get("wg_public_key") or node.get("public_key") or "",
                      "allowed_ips": f"{node['host']}/32",
                      "endpoint": f"{node['host']}:{node.get('wg_port', 51820)}",
                      "persistent_keepalive": 25})
    return [p for p in peers if p["public_key"]]


def up(name: str, config_text: str) -> bool:
    """Bring up interface `name` from `config_text`. No-op (returns False) unless the operator
    enabled VPN and WireGuard is installed — never fails the job over networking."""
    if not vpn_enabled() or not wg_available():
        return False
    try:
        os.makedirs(WG_DIR, exist_ok=True)
        path = os.path.join(WG_DIR, f"{name}.conf")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(config_text)
        subprocess.run(["wg-quick", "up", path], check=True, capture_output=True)
        return True
    except Exception:
        return False


def down(name: str) -> bool:
    """Tear the interface down. Idempotent + safe when it was never up."""
    if not wg_available():
        return False
    path = os.path.join(WG_DIR, f"{name}.conf")
    try:
        subprocess.run(["wg-quick", "down", path if os.path.exists(path) else name],
                       check=True, capture_output=True)
        return True
    except Exception:
        return False
