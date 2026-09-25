"""wireguard_test.py — the agent's WireGuard helper. Hermetic, offline, no root/wg needed.

Proves the pure logic the agent relies on:
  * a keypair is valid base64 Curve25519 (32 bytes each), even with no `wg` installed;
  * the interface config renders correctly (Interface + Peer stanzas);
  * cluster peers become [Peer] entries for every OTHER rank;
  * VPN is fail-closed: up() no-ops unless the operator opted in AND wg is present;
  * is_vpn_job() recognises a buyer VPN choice and a distributed cluster.

Run: python wireguard_test.py
"""
import base64
import os

os.environ.pop("AGENT_VPN_ENABLED", None)   # default OFF for the fail-closed checks

import wireguard as wg  # noqa: E402

_fail = 0


def ok(label, cond):
    global _fail
    print(("ok  " if cond else "FAIL") + "  " + label)
    if not cond:
        _fail += 1


# keypair: valid base64, 32-byte raw keys, private != public
priv, pub = wg.gen_keypair()
ok("gen_keypair returns two distinct base64 keys", priv != pub and len(priv) > 0)
ok("keys decode to 32-byte Curve25519 material",
   len(base64.b64decode(priv)) == 32 and len(base64.b64decode(pub)) == 32)

# interface rendering (pure)
cfg = wg.render_interface(priv, "10.0.0.5/32", dns="1.1.1.1", peers=[
    {"public_key": "SRVPUB==", "allowed_ips": "0.0.0.0/0",
     "endpoint": "vpn.example:51820", "persistent_keepalive": 25}])
ok("rendered config has an [Interface] with our private key + address",
   "[Interface]" in cfg and f"PrivateKey = {priv}" in cfg and "Address = 10.0.0.5/32" in cfg)
ok("rendered config has the [Peer] with pubkey, allowed-ips, endpoint, keepalive",
   "[Peer]" in cfg and "PublicKey = SRVPUB==" in cfg and "AllowedIPs = 0.0.0.0/0" in cfg
   and "Endpoint = vpn.example:51820" in cfg and "PersistentKeepalive = 25" in cfg)

# cluster -> peers: every OTHER rank becomes a peer at its VPN address
cluster = {"nodes": [
    {"rank": 0, "host": "10.0.0.1", "wg_public_key": "K0=", "wg_port": 51820},
    {"rank": 1, "host": "10.0.0.2", "wg_public_key": "K1=", "wg_port": 51820},
    {"rank": 2, "host": "10.0.0.3", "wg_public_key": "K2=", "wg_port": 51820},
]}
peers = wg.peers_from_cluster(cluster, my_rank=1)
ok("peers_from_cluster excludes my own rank and includes the others",
   len(peers) == 2 and all(p["public_key"] in ("K0=", "K2=") for p in peers))
ok("each cluster peer is a /32 at its VPN host:port",
   all(p["allowed_ips"].endswith("/32") and ":51820" in p["endpoint"] for p in peers))
ok("a peer with no wg key is dropped (can't form a tunnel without one)",
   wg.peers_from_cluster({"nodes": [{"rank": 9, "host": "10.0.0.9"}]}, my_rank=1) == [])

# is_vpn_job: buyer choice OR distributed cluster
ok("is_vpn_job true when the buyer chose VPN", wg.is_vpn_job({"vpn": True}) is True)
ok("is_vpn_job true for a distributed cluster rank",
   wg.is_vpn_job({"egress": "cluster"}) is True and wg.is_vpn_job({"distributed": {"rank": 0}}) is True)
ok("is_vpn_job false for an ordinary job", wg.is_vpn_job({"task_type": "notebook"}) is False)

# fail-closed: up() must NOT touch the interface unless the operator opted in
os.environ["AGENT_VPN_ENABLED"] = "false"
ok("up() no-ops when VPN is not enabled on this node (fail-closed)",
   wg.up("pbtest0", cfg) is False)
os.environ["AGENT_VPN_ENABLED"] = "true"
if not wg.wg_available():
    ok("up() still no-ops when WireGuard is not installed (never fails the job)",
       wg.up("pbtest0", cfg) is False)
else:
    ok("wg present: skipping destructive up() in the hermetic test", True)
os.environ.pop("AGENT_VPN_ENABLED", None)

print(f"\n=== wireguard(agent): {'0 failures' if _fail == 0 else str(_fail) + ' FAILED'} ===")
raise SystemExit(1 if _fail else 0)
