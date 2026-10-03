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

# ---- direct in-country storage bypass (PB_EGRESS_DIRECT_HOSTS) ----------------------------------
import types

_calls, _dockeruser = [], []          # every command run; fake `iptables -S DOCKER-USER` listing
LIST_FAIL = False                     # make that listing fail (xtables lock, missing chain...)


def _fake_run(args, *a, **k):
    _calls.append(list(args))
    out = ""
    if args[:3] == ["iptables", "-S", "DOCKER-USER"]:
        if LIST_FAIL:
            return types.SimpleNamespace(returncode=4, stdout="", stderr="xtables lock")
        out = "\n".join(_dockeruser)
    # every -C probe misses, so the applied command list is visible in _calls; the cleanup's
    # `ip rule del from SUB` loop ends after one pass
    miss = "-C" in args or (args[:3] == ["ip", "rule", "del"] and _calls.count(list(args)) > 1)
    return types.SimpleNamespace(returncode=1 if miss else 0, stdout=out, stderr="")


_real_run, _real_gai = ev.subprocess.run, ev.socket.getaddrinfo
ev.subprocess.run = _fake_run
DNS = {"s3.oss-me-central-1.aliyuncs.com": ["47.91.100.1", "47.91.100.2"],
       "evil.oss-me-central-1.aliyuncs.com": ["169.254.169.254", "10.0.0.5", "127.0.0.1",
                                              "100.64.1.1", "224.0.0.1", "47.91.100.3"]}


def _gai(host, *a, **k):
    if host not in DNS:
        raise OSError("NXDOMAIN")
    return [(2, 1, 6, "", (ip, 443)) for ip in DNS[host]]


ev.socket.getaddrinfo = _gai
_real_wan = ev._wan_iface
ev._wan_iface = lambda: WAN


def applied():
    """Commands that change state (drops -C probes, `ip rule` / `iptables -S` listings)."""
    return [c for c in _calls if c[:3] == ["ip", "rule", "add"]
            or ("-C" not in c and "-S" not in c and c[:2] != ["ip", "rule"])]

# OFF: byte-for-byte today's rules (no DNS, no extra rule)
os.environ.pop("PB_EGRESS_DIRECT_HOSTS", None)
_calls.clear()
ev.route_bridge(SUB, WAN)
ok("direct OFF: route_bridge applies exactly bridge_commands", applied() == ev.bridge_commands(SUB, WAN))
os.environ["PB_EGRESS_DIRECT_HOSTS"] = ""
_calls.clear()
ev.route_bridge(SUB, WAN)
ok("direct empty string == off", applied() == ev.bridge_commands(SUB, WAN))

# hostname validation + cap
ok("bad hostnames refused", ev.direct_hosts("evil.com, aliyuncs.com.evil.com, x.aliyuncs.com\n, "
                                            "-a.aliyuncs.com, 1.2.3.4, s3.oss-me-central-1.aliyuncs.com,"
                                            "x.aliyuncs.com.") == ["x.aliyuncs.com",
                                                                     "s3.oss-me-central-1.aliyuncs.com"])
ok("host cap enforced", len(ev.direct_hosts(",".join(f"b{i}.oss-me-central-1.aliyuncs.com"
                                                     for i in range(50)))) == ev.MAX_DIRECT_HOSTS)
# private/metadata/CGNAT/loopback/multicast answers never become a bypass
ok("private/metadata DNS answers refused",
   ev.direct_ips(["evil.oss-me-central-1.aliyuncs.com"]) == ["47.91.100.3"])
ok("DNS failure = no bypass", ev.direct_ips(["nx.aliyuncs.com"]) == [])
DNS["many.aliyuncs.com"] = [f"47.92.0.{i}" for i in range(1, 100)]
ok("IP cap enforced", len(ev.direct_ips(["many.aliyuncs.com"])) == ev.MAX_DIRECT_IPS)
try:
    ev.direct_commands(SUB, WAN, ["169.254.169.254"])
    ok("direct_commands refuses a metadata IP", False)
except ValueError:
    ok("direct_commands refuses a metadata IP", True)

# ON: per-IP main-table rule + ACCEPT inserted ABOVE the fail-closed DROP
os.environ["PB_EGRESS_DIRECT_HOSTS"] = "s3.oss-me-central-1.aliyuncs.com,evil.com"
_calls.clear()
ev.route_bridge(SUB, WAN)
got = applied()
drop = ["iptables", "-I", "DOCKER-USER", "1", "-s", SUB, "-o", WAN, "-j", "DROP"]
acc = lambda ip: ["iptables", "-I", "DOCKER-USER", "1", "-s", SUB, "-d", ip + "/32", "-o", WAN,
                  "-m", "comment", "--comment", "pb-egress-direct", "-j", "ACCEPT"]
rule = lambda ip: ["ip", "rule", "add", "from", SUB, "to", ip, "lookup", "main", "priority", "90"]
ok("direct ON: today's rules all still applied", got[:len(ev.bridge_commands(SUB, WAN))] == ev.bridge_commands(SUB, WAN))
ok("direct ON: exact /32 ACCEPT per resolved IP", acc("47.91.100.1") in got and acc("47.91.100.2") in got)
ok("direct ON: ACCEPT inserted after DROP (=> sits above it in DOCKER-USER)",
   got.index(drop) < got.index(acc("47.91.100.1")))
ok("direct ON: per-IP main-table rule at priority 90 (before tunnel rule 100)",
   rule("47.91.100.1") in got and rule("47.91.100.2") in got)
ok("direct ON: only the resolved IPs (no CIDR, nothing else)",
   len(got) == len(ev.bridge_commands(SUB, WAN)) + 4 and all(c[c.index("-d") + 1].endswith("/32") for c in got if "pb-egress-direct" in c))

# re-sync drops a stale IP (ACCEPT first, then its route) and keeps current ones
_dockeruser[:] = [f"-A DOCKER-USER -s {SUB} -d {ip}/32 -o {WAN} -m comment --comment pb-egress-direct -j ACCEPT"
                  for ip in ("47.91.100.1", "47.91.100.9")]
_calls.clear()
ev.sync_direct(SUB, WAN)
dele = ["iptables", "-D", "DOCKER-USER", "-s", SUB, "-d", "47.91.100.9/32", "-o", WAN,
        "-m", "comment", "--comment", "pb-egress-direct", "-j", "ACCEPT"]
rdel = ["ip", "rule", "del", "from", SUB, "to", "47.91.100.9", "lookup", "main", "priority", "90"]
ok("re-sync removes stale IP ACCEPT then route", dele in _calls and rdel in _calls
   and _calls.index(dele) < _calls.index(rdel))
ok("re-sync keeps current IPs", not any(c[:2] == ["iptables", "-D"] and "47.91.100.1/32" in c for c in _calls))

# feature turned off at the API -> heartbeat clears the bypass on live bridges
import tempfile
ev.STATE_DIR = tempfile.mkdtemp()
ev.record(42, SUB)
_calls.clear()
ev.note_direct_hosts("")
ok("heartbeat '' removes every direct ACCEPT on live bridges",
   dele in _calls and any(c[:2] == ["iptables", "-D"] and "47.91.100.1/32" in c for c in _calls))
ok("heartbeat '' adds nothing", not any(c[:3] == ["ip", "rule", "add"] or "-I" in c for c in _calls))

# cleanup: removing the bridge removes its direct ACCEPTs too (ip rules go with `del from SUB`)
_calls.clear()
ev.unroute_for_tid(42)
ok("cleanup removes direct ACCEPTs", dele in _calls)
ok("cleanup removes ip rules from the bridge", ["ip", "rule", "del", "from", SUB] in _calls)
ok("cleanup still removes the fail-closed DROP",
   ["iptables", "-D", "DOCKER-USER", "-s", SUB, "-o", WAN, "-j", "DROP"] in _calls)
ok("cleanup forgets the bridge", not ev._recorded_subnets())

# a hung resolver: timed-out refreshes re-await the in-flight lookup on one shared fixed-size pool,
# instead of leaving another blocked thread behind on every heartbeat / bridge setup
import threading
_gate, _started = threading.Event(), []


def _hung_gai(host, *a, **k):
    _started.append(host)
    _gate.wait(10)
    return _gai(host, *a, **k)


ev.socket.getaddrinfo = _hung_gai
DNS["slow.aliyuncs.com"], DNS["slow2.aliyuncs.com"] = ["47.91.100.7"], ["47.91.100.8"]
SLOW = ["slow.aliyuncs.com", "slow2.aliyuncs.com"]
ok("hung resolver: a timed-out lookup = no bypass",
   [ev.direct_ips(SLOW, timeout=0.05) for _ in range(4)] == [[]] * 4)
ok("hung resolver: at most one lookup in flight per host (no thread pile-up)", sorted(_started) == SLOW)
_gate.set()
ok("hung resolver: recovers once DNS answers", ev.direct_ips(SLOW, timeout=2) == ["47.91.100.7", "47.91.100.8"])
ev.socket.getaddrinfo = _gai

# a failed DOCKER-USER listing is a failed sync (never read as "no bypass installed"), and the
# heartbeat retries it on the very next beat instead of after DIRECT_REFRESH_S
LIST_FAIL = True
try:
    ev.sync_direct(SUB, WAN, ips=[])
    ok("a failed DOCKER-USER listing fails the sync", False)
except RuntimeError:
    ok("a failed DOCKER-USER listing fails the sync", True)
ev.record(43, SUB)                                   # live bridge; _dockeruser still has .1 and .9
os.environ["PB_EGRESS_DIRECT_HOSTS"] = "s3.oss-me-central-1.aliyuncs.com"
ev._direct_at = ev.time.time()                       # it was just synced
try:
    ev.note_direct_hosts("")                         # API clears the list while iptables fails
except RuntimeError:
    pass
LIST_FAIL = False
_calls.clear()
ev.note_direct_hosts("")                             # next beat, same (cleared) list
ok("heartbeat retries a failed sync on the next beat (bypass removed)", dele in _calls)

# an empty host list still reconciles: a bypass left on a reused bridge subnet (agent restarted
# without cleanup) is removed when that subnet is routed again
_calls.clear()
ev.route_bridge(SUB, WAN)
ok("route_bridge with no direct hosts removes a leftover direct ACCEPT", dele in _calls)
ok("…and adds no bypass", not any("pb-egress-direct" in c and "-I" in c for c in _calls))

# teardown stays best-effort when the listing fails
LIST_FAIL = True
_calls.clear()
ev.unroute_for_tid(43)
LIST_FAIL = False
ok("failed listing: teardown still removes the DROP and forgets the bridge",
   ["iptables", "-D", "DOCKER-USER", "-s", SUB, "-o", WAN, "-j", "DROP"] in _calls
   and not ev._recorded_subnets())
ev.subprocess.run, ev.socket.getaddrinfo, ev._wan_iface = _real_run, _real_gai, _real_wan
os.environ.pop("PB_EGRESS_DIRECT_HOSTS", None)
_dockeruser.clear()

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
   _up == [["wg-quick", "up", os.path.join(ev.STATE_DIR, "wg-egress.conf")],
           ["ip", "route", "replace", "10.9.0.1/32", "dev", "wg-egress"]])

print("\n=== egress_vpn: " + ("0 failures" if _fail == 0 else str(_fail) + " FAILED") + " ===")
raise SystemExit(1 if _fail else 0)
