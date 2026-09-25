"""egress_vpn command-builder + gating — offline, no root/network. Run: python egress_vpn_test.py

Verifies the exact rules that force a buyer bridge's INTERNET egress through the gateway tunnel and
FAIL CLOSED: local/private dsts stay in the main table, everything else goes to the tunnel table,
the bridge is SNAT'd into the tunnel, MSS is clamped BOTH directions, and a DROP guarantees the
bridge can never egress out the seller NIC directly.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
for k in ("PB_EGRESS_GATEWAY_PUBKEY", "PB_EGRESS_GATEWAY_ENDPOINT", "PB_EGRESS_ADDR", "PB_EGRESS_MTU"):
    os.environ.pop(k, None)
import egress_vpn as ev

_fail = 0


def ok(name, cond):
    global _fail
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        _fail += 1


# gating: off until fully configured (existing nodes unaffected)
ok("disabled with no config", not ev.enabled())
os.environ["PB_EGRESS_GATEWAY_PUBKEY"] = "GWPUB="
os.environ["PB_EGRESS_GATEWAY_ENDPOINT"] = "1.2.3.4:51821"
ok("still disabled until an address is assigned", not ev.enabled())
os.environ["PB_EGRESS_ADDR"] = "10.9.0.7/32"
ok("enabled once gateway+endpoint+addr are set", ev.enabled())

os.environ["PB_EGRESS_MTU"] = "1420"
ok("mss = mtu - 40", ev._mss() == 1380)

SUB, WAN = "172.30.0.0/24", "eth0"
cmds = ev.bridge_commands(SUB, WAN)
joined = [" ".join(c) for c in cmds]


def has(sub):
    return any(sub in j for j in joined)


# fail-closed: a DROP for bridge -> WAN, inserted at the top of DOCKER-USER
ok("FAIL-CLOSED drop bridge->WAN present",
   ["iptables", "-I", "DOCKER-USER", "1", "-s", SUB, "-o", WAN, "-j", "DROP"] in cmds)
# tunnel table default + the catch-all internet rule
ok("default route via the tunnel in the dedicated table",
   ["ip", "route", "replace", "default", "dev", "wg-egress", "table", "51821"] in cmds)
ok("internet-bound (priority 100) -> tunnel table",
   ["ip", "rule", "add", "from", SUB, "lookup", "51821", "priority", "100"] in cmds)
# local/private dsts exempted to the main table (inbound publish + docker-proxy return path)
ok("loopback exempted to main", has("to 127.0.0.0/8 lookup main"))
ok("RFC1918 10/8 exempted to main", has("to 10.0.0.0/8 lookup main"))
ok("cloud metadata 169.254 exempted to main", has("to 169.254.0.0/16 lookup main"))
ok("bridge self exempted to main", has("to 172.30.0.0/24 lookup main"))
ok("all exemptions are priority 90 (before the tunnel rule)",
   all("priority 90" in j for j in joined if "lookup main" in j))
# SNAT the bridge into the tunnel
ok("SNAT bridge -> wg-egress",
   ["iptables", "-t", "nat", "-A", "POSTROUTING", "-s", SUB, "-o", "wg-egress", "-j", "MASQUERADE"] in cmds)
# MSS clamp BOTH directions
ok("MSS clamp outbound (-o wg-egress)", has("-o wg-egress -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --set-mss 1380"))
ok("MSS clamp inbound (-i wg-egress)", has("-i wg-egress -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --set-mss 1380"))

# junk subnet is rejected (never build rules for an unvalidated string)
try:
    ev.bridge_commands("not-a-subnet", "eth0")
    ok("invalid subnet rejected", False)
except (ValueError, Exception):
    ok("invalid subnet rejected", True)

# regression: the wg conf must NOT be written under /etc (read-only in the agent's systemd sandbox,
# ProtectSystem=full -> EROFS failed every per-job network). It goes under the writable STATE_DIR.
import tempfile
_tmp = tempfile.mkdtemp()
ev.WG_DIR, ev.NODE_KEY = "/nonexistent-ro-etc", os.path.join(_tmp, "key")
ev.STATE_DIR = os.path.join(_tmp, "run")
open(ev.NODE_KEY, "w").write("PRIV=")
_up = []
ev.shutil.which = lambda n: "/usr/bin/" + n
ev.node_pubkey = lambda: "PUB="
ev._iface_up = lambda: bool(_up)
ev._run = lambda args, check=True: _up.append(args)
ev.subprocess.run = lambda *a, **k: None
ok("ensure_tunnel works with a read-only WG_DIR", ev.ensure_tunnel())
ok("wg-quick brought up from STATE_DIR conf",
   _up == [["wg-quick", "up", os.path.join(ev.STATE_DIR, "wg-egress.conf")]])

print("\n=== egress_vpn: " + ("0 failures" if _fail == 0 else str(_fail) + " FAILED") + " ===")
raise SystemExit(1 if _fail else 0)
