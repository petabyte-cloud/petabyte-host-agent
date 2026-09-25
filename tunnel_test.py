"""tunnel_test.py -- the reverse tunnel must fail fast, and say why.

A node whose snapshot had no /etc/petabyte/tunnel_key claimed a serving rental, brought the
container up healthy, then spent 153 SECONDS (51 gateway ports x a flat 3s sleep) discovering that
every single attempt died instantly for the same reason -- and reported it as "FAILED to bind any
gateway port", blaming port exhaustion. It then tore down a container that was serving fine.

Only a busy port is worth trying the next port for. Everything else is about the connection and
fails identically on all of them.

Run: python tunnel_test.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_fail = 0


def ok(label, cond):
    global _fail
    print(("ok   " if cond else "FAIL ") + label)
    if not cond:
        _fail += 1


os.environ.setdefault("PETABYTE_API_URL", "http://localhost")
os.environ.setdefault("PETABYTE_API_KEY", "test")
os.environ.setdefault("PETABYTE_SPEC_ID", "1")
import task_fetcher as tf  # noqa: E402

# ------------------------------------------------------------------ which errors are retryable
ok("a refused listen port is retryable (another port may be free)",
   tf._TUN_PORT_BUSY("Warning: remote port forwarding failed for listen port 20000"))
ok("an address already in use is retryable",
   tf._TUN_PORT_BUSY("bind: Address already in use"))
ok("a rejected key is NOT retryable",
   not tf._TUN_PORT_BUSY("pbtun@10.0.0.1: Permission denied (publickey)."))
ok("a missing identity file is NOT retryable",
   not tf._TUN_PORT_BUSY("Warning: Identity file /etc/petabyte/tunnel_key not accessible"))
ok("an unreachable gateway is NOT retryable",
   not tf._TUN_PORT_BUSY("ssh: connect to host 10.0.0.1 port 22: Connection refused"))
ok("no stderr at all is NOT treated as a busy port", not tf._TUN_PORT_BUSY(""))

# ------------------------------------------------------------------ the loop honours that
_logs = []
tf.report_log = lambda tid, msg: _logs.append(msg)
tf.time.sleep = lambda _s: None          # the 3s-per-port wait is what this test exists to bound


class _Proc:
    """An ssh that died immediately with `err`, or (err=None) one that stayed up."""

    def __init__(self, err):
        self._err = err

    def poll(self):
        return None if self._err is None else 255

    def communicate(self, timeout=None):
        return ("", self._err)

    def terminate(self):
        pass


def _spawn(errs):
    """Patch _popen to hand out one _Proc per call, and count the calls."""
    seq = list(errs)
    calls = []

    def fake(cmd, capture_stderr=False):
        calls.append(cmd)
        return _Proc(seq.pop(0) if seq else "Warning: remote port forwarding failed")

    tf._popen = fake
    return calls


tf._TUN_KEY = os.path.abspath(__file__)          # exists, so the preflight passes
tf._TUN_GW = "pbtun@203.0.113.1"

_logs.clear()
calls = _spawn(["pbtun@203.0.113.1: Permission denied (publickey)."])
ok("a rejected key stops after ONE port instead of sleeping through 51",
   tf._open_reverse_tunnel(9000, 1) is None and len(calls) == 1)
ok("and the log says it was refused, not that ports ran out",
   any("refused by the gateway" in m for m in _logs))

_logs.clear()
calls = _spawn(["Warning: remote port forwarding failed for listen port 20000", None])
ok("a busy port DOES move on to the next one",
   tf._open_reverse_tunnel(9000, 2) == tf._tun_port_range()[1] and len(calls) == 2)
ok("a tunnel that comes up is reported as up", any("tunnel up" in m for m in _logs))

_logs.clear()
tf._TUN_KEY = "/nonexistent/tunnel_key"
calls = _spawn([None])
ok("a MISSING key never spawns ssh at all",
   tf._open_reverse_tunnel(9000, 3) is None and len(calls) == 0)
ok("and the log names the missing key path",
   any("/nonexistent/tunnel_key" in m and "missing" in m for m in _logs))

ok("a node with a gateway but no key does not advertise a tunnel",
   not tf._reverse_tunnel_enabled())
tf._TUN_KEY = os.path.abspath(__file__)
ok("with both halves present it does", tf._reverse_tunnel_enabled())
tf._TUN_GW = ""
ok("and a node with a key but no gateway does not", not tf._reverse_tunnel_enabled())

# A newly installed JIT node may heartbeat before its gateway key is accepted.
# Its spec must stay unsellable until the tunnel is actually usable.
os.environ["PROVIDER"] = "pb-jit-test"
tf._selling_now = lambda: True
ok("JIT node without a tunnel does not offer paid capacity", not tf._advertise_selling_now())
tf._TUN_GW = "pbtun@203.0.113.1"
ok("JIT node offers capacity after tunnel enrollment", tf._advertise_selling_now())
tf._selling_now = lambda: False
ok("JIT node still respects a closed selling window", not tf._advertise_selling_now())
os.environ["PROVIDER"] = "ordinary-seller"
tf._TUN_GW = ""
tf._selling_now = lambda: True
ok("ordinary seller schedule is unchanged", tf._advertise_selling_now())

print()
print("=== tunnel: " + ("0 failures" if _fail == 0 else str(_fail) + " FAILED") + " ===")
raise SystemExit(1 if _fail else 0)
