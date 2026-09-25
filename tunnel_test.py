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
        # the agent drains ssh's stderr: OpenSSH's bind confirmation, or the line that says why
        self.stderr = iter(["debug1: remote forward success for: listen 127.0.0.1:20001\n"]
                           if err is None else [err + "\n"])

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

# ------------------------------------------------------------------ "up" means the bind was confirmed
import json  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
import types  # noqa: E402
import subprocess as _sp  # noqa: E402
import execution_receipt as _er  # noqa: E402

_nap = threading.Event().wait                 # time.sleep is stubbed above; this one really waits
tf._TUN_GW = "pbtun@203.0.113.1"
tf._TUN_PORTS_FILE = os.path.join(tempfile.mkdtemp(prefix="pb-tun-"), "tunnel_ports.json")
tf._tun_sup_started.set()                     # drive _check_tunnels by hand, no background thread
tf._pb_vm_started["on"] = True                # ...and no container-watchdog thread either


class _Connecting(_Proc):
    """ssh still dialing an unreachable gateway for `n` polls, then killed by ConnectTimeout."""

    def __init__(self, n, err):
        super().__init__(err)
        self._n = n

    def poll(self):
        self._n -= 1
        return None if self._n >= 0 else 255


calls = []
tf._popen = lambda cmd, capture_stderr=False: (calls.append(cmd), _Connecting(
    3, "ssh: connect to host 203.0.113.1 port 22: Connection timed out"))[1]
_logs.clear()
ok("an ssh still connecting after 3s is NOT a tunnel (the old check registered a dead port)",
   tf._open_reverse_tunnel(9000, 10) is None and len(calls) == 1)
ok("ssh gets a ConnectTimeout and exits when the gateway refuses the forward",
   "ConnectTimeout=10" in calls[0] and "ExitOnForwardFailure=yes" in calls[0])
ok("and the log carries ssh's own reason", any("Connection timed out" in m for m in _logs))

_saved_confirm = tf._TUN_CONFIRM_S
tf._TUN_CONFIRM_S = 1
_quiet = _Proc(None)
_quiet.stderr = iter([])                      # an ssh that never prints the confirmation line
tf._popen = lambda cmd, capture_stderr=False: _quiet
ok("an ssh that stays up past the confirm window without the line is still accepted (fallback)",
   tf._open_reverse_tunnel(9000, 12) == tf._tun_port_range()[0])
tf._TUN_CONFIRM_S = _saved_confirm
tf._kill_reverse_tunnel(12)

# ------------------------------------------------------------------ stderr is drained for life
_read = []


def _noise():
    yield "debug1: remote forward success for: listen 127.0.0.1:20000, connect 127.0.0.1:9000\n"
    for i in range(5000):                     # far more than a 64 KB pipe holds
        _read.append(i)
        yield "debug1: channel 3: free: 127.0.0.1, nchannels 4\n"


_chatty = _Proc(None)
_chatty.stderr = _noise()
tf._popen = lambda cmd, capture_stderr=False: _chatty
ok("a tunnel comes up on ssh's confirmation", tf._open_reverse_tunnel(9000, 11) is not None)
for _ in range(200):
    if len(_read) == 5000:
        break
    _nap(0.01)
ok("its stderr keeps being drained after it is up (an unread PIPE blocks ssh at ~64 KB)",
   len(_read) == 5000)
tf._kill_reverse_tunnel(11)


# ------------------------------------------------------------------ orphan reap kills the tunnel
class _Killable(_Proc):
    def __init__(self):
        super().__init__(None)
        self.killed = False

    def terminate(self):
        self.killed = True


_k = _Killable()
tf._tunnels[200] = (20003, _k)
tf._kill_reverse_tunnel("200")                # the orphan reap passes the docker label: a str
ok("a str task id (orphan reap) still kills the int-keyed tunnel", _k.killed and 200 not in tf._tunnels)

# ------------------------------------------------------------------ supervision: re-open + re-register
tf._tunnels.clear()
tf._tun_rentals.clear()
_reg = []
tf._register_vm_tunnel = lambda vm_id, port, ip_address=None, attempts=5: (
    _reg.append((vm_id, port, ip_address)), True)[1]
with tf._pb_vm_lock:
    tf._pb_vm_watch[5] = {"name": "c5", "reported": False}
    tf._pb_vm_watch[6] = {"name": "c6", "reported": False}
tf._tunnels[6] = (20000, _Proc(None))
tf._supervise_tunnel(6, "c6", 9006, "vm_6", rp=20000, registered=True)
tf._tunnels[5] = (20001, _Proc("Connection reset by peer"))      # ssh already exited
tf._supervise_tunnel(5, "c5", 9005, "vm_5", rp=20001, registered=True)
calls = _spawn([None])
tf._check_tunnels(now=1000.0)
ok("a dropped tunnel is re-opened on the SAME gateway port",
   len(calls) == 1 and "127.0.0.1:20001:127.0.0.1:9005" in calls[0])
ok("and re-registered with the API", _reg == [("vm_5", 20001, "127.0.0.1")])
tf._check_tunnels(now=1001.0)
ok("healthy tunnels are left alone", len(calls) == 1 and len(_reg) == 1)
ok("the ports are saved for an agent restart",
   json.load(open(tf._TUN_PORTS_FILE)) == {"5": 20001, "6": 20000})

calls = _spawn([None])
tf._open_reverse_tunnel(9007, 7)
ok("a new tunnel never takes a port another rental's route points at",
   "127.0.0.1:20002:127.0.0.1:9007" in calls[0])
tf._kill_reverse_tunnel(7)

with tf._pb_vm_lock:
    tf._pb_vm_watch.pop(6)                    # rental 6 ended
tf._check_tunnels(now=1002.0)
ok("an ended rental stops being supervised and its tunnel is killed",
   6 not in tf._tun_rentals and 6 not in tf._tunnels)

# gateway unreachable: back off, then give up and fail the rental
tf._tunnels[5] = (20001, _Proc("Connection reset by peer"))
calls = _spawn(["ssh: connect to host 203.0.113.1 port 22: Connection refused"] * 50)
t0 = 5000.0
tf._check_tunnels(now=t0)
tf._check_tunnels(now=t0 + 1)
ok("a failed re-open backs off instead of hammering the gateway", len(calls) == 1)
tf._check_tunnels(now=t0 + tf._TUN_RETRY_MIN_S + 1)
ok("and retries once the backoff passes", len(calls) == 2)
ok("the rental is not failed inside the give-up window", not tf._pb_vm_watch[5].get("fail"))
tf._check_tunnels(now=t0 + tf._TUN_GIVE_UP_S + 1)
ok("past the give-up window it is handed to the watchdog as failed",
   tf._pb_vm_watch[5].get("fail") == "tunnel_lost" and 5 not in tf._tun_rentals)

_posted, _cleaned = [], []
tf._post_result_ack = lambda payload: (_posted.append(payload), True)[1]
tf._signed_result = lambda tid, status="completed", result=None, **k: {
    "task_id": tid, "status": status, "result": result}
tf._cleanup_job_resources = lambda tid, name=None: _cleaned.append((tid, name))
_er.forget = lambda tid: None
tf._pb_vm_scan()
ok("the watchdog reports it failed (buyer refunded/prorated) without asking docker",
   len(_posted) == 1 and _posted[0]["status"] == "failed" and "tunnel_lost" in _posted[0]["result"])
ok("and tears the rental down", _cleaned == [(5, "c5")] and 5 not in tf._pb_vm_watch)

# ------------------------------------------------------------------ restored after an agent restart
json.dump({"41": 20004}, open(tf._TUN_PORTS_FILE, "w"))
tf._tun_rentals.clear()
tf._tunnels.clear()
_labels = {"cNew": "||vm_41|9041", "cOld": "<no value>|<no value>|<no value>|<no value>"}


def _docker(cmd, **kw):
    if cmd[:2] == ["docker", "ps"]:
        return types.SimpleNamespace(returncode=0, stdout="cNew\ncOld\n")
    return types.SimpleNamespace(returncode=0, stdout=_labels[cmd[-1]] + "\n")


tf._container_label_task = lambda cid: {"cNew": "41", "cOld": "42"}[cid]
_er.knows = lambda tid: True
_real_run, _sp.run = _sp.run, _docker
try:
    tf._restore_vm_watch()
finally:
    _sp.run = _real_run
_s = tf._tun_rentals.get(41) or {}
ok("a restarted agent re-supervises the rental's tunnel from its container labels",
   _s.get("host_port") == 9041 and _s.get("vm_id") == "vm_41")
ok("preferring the gateway port it held before the restart", _s.get("rp") == 20004)
ok("an older agent's container (no labels) is skipped, but still watched",
   42 not in tf._tun_rentals and 42 in tf._pb_vm_watch)
calls = _spawn([None])
_reg.clear()
tf._check_tunnels(now=9000.0)
ok("the next pass re-opens it on that port and re-registers it",
   "127.0.0.1:20004:127.0.0.1:9041" in calls[0] and _reg == [("vm_41", 20004, "127.0.0.1")])

print()
print("=== tunnel: " + ("0 failures" if _fail == 0 else str(_fail) + " FAILED") + " ===")
raise SystemExit(1 if _fail else 0)
