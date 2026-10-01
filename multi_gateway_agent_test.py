"""multi_gateway_agent_test.py -- the agent publishes each rental on the rental's OWN gateway.

The API stores a reverse-tunnelled rental's route as 127.0.0.1:<port> on ONE gateway (the box its
<id>.<zone> resolves to). With two gateways (us, sa = Riyadh) the agent must:
  * open that rental's `ssh -R` to the gateway the job payload names (the default one = its own
    enrolled/hand-configured gateway), never to some other box;
  * tell the API which gateway it reached (register_tunnel gateway_id), on first registration and
    on every supervised re-open;
  * keep that gateway across an agent restart (container labels);
  * refuse, rather than mis-route, a rental whose gateway it cannot name;
  * adopt only well-formed gateway lists from the API, and report its per-gateway support + RTT;
  * re-point its WireGuard egress peer when the API moves it to its home gateway, and remember it.

Run: python multi_gateway_agent_test.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_fail = 0


def ok(label, cond, extra=""):
    global _fail
    print(("ok   " if cond else "FAIL ") + label + (f"   [{extra}]" if extra and not cond else ""))
    if not cond:
        _fail += 1


os.environ.setdefault("PETABYTE_API_URL", "http://localhost")
os.environ.setdefault("PETABYTE_API_KEY", "test")
os.environ.setdefault("PETABYTE_SPEC_ID", "1")
import task_fetcher as tf  # noqa: E402
import egress_vpn  # noqa: E402

tf.report_log = lambda tid, msg: None
tf._save_tunnel_ports = lambda: None
SA = {"id": "sa", "target": "pbtun@130.94.59.215"}

# ------------------------------------------------------------------ gateway list from the API
tf._TUN_GWS.clear()
tf._note_gateways([{"id": "us", "target": "pbtun@203.0.113.9"}, SA,
                   {"id": "evil", "target": "-oProxyCommand=sh pbtun@x"},
                   {"id": "x" * 40, "target": "pbtun@1.2.3.4"}, "junk"])
ok("well-formed gateways are adopted", tf._TUN_GWS == {"us": "pbtun@203.0.113.9", "sa": "pbtun@130.94.59.215"},
   str(tf._TUN_GWS))
tf._note_gateways([])
ok("an empty list does not wipe the known gateways", "sa" in tf._TUN_GWS)

# ------------------------------------------------------------------ which gateway a rental uses
tf._TUN_GW = "pbtun@203.0.113.9"
ok("no tunnel_gateway (older API) -> the node's own default gateway",
   tf._rental_gateway({}) == ("us", None))
ok("tunnel_gateway us -> the node's own default (its exact enrolled target)",
   tf._rental_gateway({"tunnel_gateway": {"id": "us", "target": "pbtun@elsewhere"}}) == ("us", None))
ok("tunnel_gateway sa -> that gateway's target", tf._rental_gateway({"tunnel_gateway": SA}) == ("sa", SA["target"]))
ok("a known gateway with no target in the payload uses the heartbeat's list",
   tf._rental_gateway({"tunnel_gateway": {"id": "sa"}}) == ("sa", SA["target"]))
ok("an option-smuggling target is never used",
   tf._rental_gateway({"tunnel_gateway": {"id": "zz", "target": "-oProxyCommand=x"}}) == ("zz", ""))


# ------------------------------------------------------------------ ssh -R goes to that gateway
class _Proc:
    stderr = iter(["debug1: remote forward success for: listen 127.0.0.1:20000\n"])

    def poll(self):
        return None

    def terminate(self):
        pass


calls = []
tf._popen = lambda cmd, capture_stderr=False: (calls.append(cmd), _Proc())[1]
tf._TUN_KEY = os.path.abspath(__file__)
rp = tf._open_reverse_tunnel(9000, 11, gateway=SA["target"])
ok("the rental's tunnel dials the SA gateway", rp and calls[-1][-1] == SA["target"], str(calls[-1][-3:]))
_Proc.stderr = iter(["debug1: remote forward success\n"])
tf._open_reverse_tunnel(9001, 12)
ok("a default-gateway rental still dials the node's own gateway", calls[-1][-1] == tf._TUN_GW)
n = len(calls)
ok("a gateway the node cannot name opens nothing (never falls back to the default box)",
   tf._open_reverse_tunnel(9002, 13, gateway="") is None and len(calls) == n)

# ------------------------------------------------------------------ register_tunnel names it
posted = []


class _Resp:
    status_code = 200
    text = ""


tf.httpx.post = lambda url, **kw: (posted.append((url, kw.get("json"))), _Resp())[1]
tf._register_vm_tunnel("vmsa0000001", 20000, ip_address="127.0.0.1", gateway_id="sa")
ok("registration names the gateway the tunnel reached", posted[-1][1].get("gateway_id") == "sa", str(posted[-1]))
tf._register_vm_tunnel("vmus0000001", 20001, ip_address="127.0.0.1")
ok("a default rental says us", posted[-1][1].get("gateway_id") == "us")

tf._tun_rentals.clear()
tf._tun_sup_started.set()                      # no background supervisor thread in this test
tf._supervise_tunnel(21, "c21", 9100, "vmsa0000002", rp=None, gw=("sa", SA["target"]))
ok("the supervisor remembers the rental's gateway", tf._tun_rentals[21]["gw_id"] == "sa"
   and tf._tun_rentals[21]["gw_target"] == SA["target"])
seen = {}
tf._open_reverse_tunnel = lambda hp, tid, prefer=None, gateway=None: (seen.update(gateway=gateway), 20005)[1]
with tf._pb_vm_lock:
    tf._pb_vm_watch[21] = {"name": "c21"}     # a live rental (not reported, not failed)
tf._check_tunnels(now=10**9)
ok("a dropped tunnel is re-opened to the SAME gateway", seen.get("gateway") == SA["target"], str(seen))
ok("…and re-registered naming it", posted[-1][1].get("gateway_id") == "sa"
   and posted[-1][1].get("vm_id") == "vmsa0000002", str(posted[-1]))

# ------------------------------------------------------------------ restart keeps the gateway
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "task_fetcher.py")).read()
ok("the container is labelled with its gateway", 'f"pb.gateway={_lgw[0]}"' in src and 'f"pb.gateway_target={_lgw[1]}"' in src)
ok("a restarted agent reads that label back", '"pb.gateway", "pb.gateway_target"' in src)

# ------------------------------------------------------------------ heartbeat report
tf._GW_PROBE.update(at=0.0, value={"us": 180.0, "sa": 4.2}, thread=None)
tf.threading.Thread = type("T", (), {"__init__": lambda s, **k: None, "start": lambda s: None,
                                      "is_alive": lambda s: False})
rep = tf._gateway_report()
ok("the heartbeat declares per-rental gateway support", rep["version"] == 1)
ok("…with the last measured RTT to each gateway", rep["tcp_ms"] == {"us": 180.0, "sa": 4.2})

# ------------------------------------------------------------------ egress gateway switch
SA_WG = "S" * 42 + "A="                       # a 32-byte key's base64 (last char before '=')
tmp = tempfile.mkdtemp()
egress_vpn.GATEWAY_OVERRIDE = os.path.join(tmp, "egress_gateway.json")
os.environ.update(PB_EGRESS_ADDR="10.9.0.7/32", PB_EGRESS_GATEWAY_PUBKEY="U" * 43 + "=",
                  PB_EGRESS_GATEWAY_ENDPOINT="203.0.113.9:51821")
ran = []
egress_vpn._run = lambda args, check=True: ran.append(args) or type("R", (), {"returncode": 0, "stdout": ""})()
egress_vpn._iface_up = lambda: True
egress_vpn.shutil.which = lambda _x: "/usr/bin/wg"
egress_vpn.ensure_tunnel = lambda: True
ok("a malformed key/endpoint is refused", not egress_vpn.switch_gateway("nope", "1.2.3.4:51821")
   and not egress_vpn.switch_gateway(SA_WG, "1.2.3.4:99999"))
ok("a valid switch succeeds", egress_vpn.switch_gateway(SA_WG, "130.94.59.215:51821"))
ok("…removing the old peer and adding the new one on the live interface",
   ["wg", "set", "wg-egress", "peer", "U" * 43 + "=", "remove"] in ran
   and any(a[:5] == ["wg", "set", "wg-egress", "peer", SA_WG] and "130.94.59.215:51821" in a for a in ran))
ok("…and this process now routes through it", os.environ["PB_EGRESS_GATEWAY_ENDPOINT"] == "130.94.59.215:51821")
os.environ["PB_EGRESS_GATEWAY_ENDPOINT"] = "203.0.113.9:51821"
egress_vpn.load_gateway_override()
ok("the choice survives an agent restart", os.environ["PB_EGRESS_GATEWAY_ENDPOINT"] == "130.94.59.215:51821"
   and json.load(open(egress_vpn.GATEWAY_OVERRIDE))["pubkey"] == SA_WG)

print(f"\n{'ALL PASS' if not _fail else f'{_fail} FAILED'}")
raise SystemExit(1 if _fail else 0)
