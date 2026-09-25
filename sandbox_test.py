"""sandbox_test.py — the buyer-job containers protect the SELLER's machine.

A buyer's workload runs on the seller's hardware. These tests assert the agent launches
EVERY buyer container with the notebook-grade hardening flags (drop all Linux capabilities,
no-new-privileges, pids/mem caps) and the right network posture, so a malicious job cannot
reconfigure the host firewall, escalate, pivot into the LAN, or steal cloud-metadata creds.

Offline: heavy agent deps (crypto/notebook/vm/telemetry) are stubbed so we can import the
real task loop and inspect the exact docker argv it builds. No docker, no network, no GPU.

Run: python sandbox_test.py
"""
import os
import os as _os
import sys
import types
import inspect

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

# task_fetcher exits at import unless the node identity is present (it's a real agent).
os.environ.setdefault("PETABYTE_API_URL", "https://test.local")
os.environ.setdefault("PETABYTE_API_KEY", "pk_test")
os.environ.setdefault("PETABYTE_SPEC_ID", "1")

# Stub the heavy/local deps so `import task_fetcher` succeeds without them.
for _name in ("crypto", "notebook", "vm", "agent_telemetry"):
    sys.modules.setdefault(_name, types.ModuleType(_name))
sys.modules["crypto"].sign_proof = lambda p: "sig"
sys.modules["notebook"].run_notebook_code = lambda *a, **k: []
sys.modules["vm"].launch_vm_task = lambda *a, **k: None

import task_fetcher as tf   # noqa: E402  (the real Linux-agent task loop)

_fail = 0


def ok(label, cond):
    global _fail
    print(("ok  " if cond else "FAIL") + "  " + label)
    if not cond:
        _fail += 1


# ---------------------------------------------------------------- _isolation_flags
base = tf._isolation_flags({})
ok("isolation drops ALL capabilities", "--cap-drop" in base and "ALL" in base)
ok("isolation sets no-new-privileges",
   "no-new-privileges" in base and "--security-opt" in base)
ok("isolation caps pids", "--pids-limit" in base)
# Fail-safe: with no server-sized limits, a CONSERVATIVE host-derived default cap is applied
# (never "no cap"), so a buyer container can't OOM-kill the host/agent or pin every CPU (audit H6).
ok("isolation applies a fail-safe memory+cpu cap when the booking sends none",
   "--memory" in base and "--cpus" in base)

# init_caps: images that start as root, chown their data dir, then drop to an unprivileged user
# (gosu/su-exec/linuxserver, most game servers) fail to boot under --cap-drop ALL ("operation not
# permitted"). The agent re-adds ONLY the server-declared minimal caps on top of the full drop —
# and NEVER a dangerous one, even if the server sends it (whitelist), so a compromised backend
# can't re-grant NET_ADMIN/SYS_ADMIN.
_ic = tf._isolation_flags({"init_caps": ["SETUID", "SETGID", "CHOWN", "NET_ADMIN", "SYS_ADMIN"]})
ok("init_caps re-adds the minimal drop caps (SETUID/SETGID/CHOWN)",
   "--cap-add" in _ic and "SETUID" in _ic and "SETGID" in _ic and "CHOWN" in _ic)
ok("init_caps NEVER honors a dangerous cap even if the server sends it",
   "NET_ADMIN" not in _ic and "SYS_ADMIN" not in _ic)
ok("init_caps is additive — --cap-drop ALL is still applied first",
   "--cap-drop" in _ic and "ALL" in _ic)
ok("no init_caps -> no cap-add (batch jobs stay fully capability-dropped)",
   "--cap-add" not in tf._isolation_flags({}))

sized = tf._isolation_flags({"memory": "8g", "cpus": 2, "pids": 512})
ok("isolation caps memory + disables swap escape when sized",
   sized.count("8g") == 2 and "--memory" in sized and "--memory-swap" in sized)
ok("isolation caps cpus when sized", "--cpus" in sized and "2" in sized)
ok("isolation honours a per-task pids limit", "512" in sized)

# STD-C: strict rootfs is OPT-IN (operator flips AGENT_STRICT_ROOTFS / AGENT_CONTAINER_USER). The
# DEFAULT profile must be byte-for-byte the pre-existing hardening — no --read-only, no --user — so
# an arbitrary buyer image (s6-overlay server, cache-writing model server) is never silently broken.
ok("strict rootfs is OFF by default (no --read-only unless the server opts in)",
   "--read-only" not in base)
ok("non-root is OFF by default (no --user unless the server opts in)", "--user" not in base)

ro = tf._isolation_flags({"read_only": True})
ok("read_only -> immutable rootfs (--read-only)", "--read-only" in ro)
ok("read_only -> a writable /tmp tmpfs so caches/scratch still work",
   "--tmpfs" in ro and any(str(x).startswith("/tmp:") for x in ro))
ok("read_only -> HOME=/tmp routes user/CUDA/pip caches onto the writable tmpfs (not the RO rootfs)",
   "-e" in ro and "HOME=/tmp" in ro)

nr = tf._isolation_flags({"run_as": "65534:65534"})
ok("run_as -> container forced non-root (--user)", "--user" in nr and "65534:65534" in nr)
# The strict flags still compose with the always-on baseline.
ok("strict profile keeps the always-on baseline (cap-drop ALL + no-new-privileges)",
   "--cap-drop" in ro and "ALL" in ro and "no-new-privileges" in ro)


# ---------------------------------------------------------------- _egress_flags
ok("egress default is CLOSED (no network)",
   tf._egress_flags({}) == ["--network", "none"])
ok("egress 'none' is closed",
   tf._egress_flags({"egress": "none"}) == ["--network", "none"])
ok("egress unknown policy fails closed",
   tf._egress_flags({"egress": "bogus"}) == ["--network", "none"])
ok("egress 'limited' opens the socket (host firewall blocks metadata/LAN)",
   tf._egress_flags({"egress": "limited"}) == [])


# ---------------------------------------------------------------- per-runner wiring
def src(fn):
    return inspect.getsource(getattr(tf, fn))


for runner in ("_run_render", "_run_transcode", "_run_template"):
    ok(f"{runner} applies the isolation flags", "_isolation_flags(task)" in src(runner))
ok("stitch concat container runs with --network none (no ffmpeg SSRF/exfil)",
   '"--network", "none"' in src("_run_stitch") and "_isolation_flags(task)" in src("_run_stitch"))
ok("render disables Blender embedded-script auto-exec",
   "--disable-autoexec" in src("_run_render"))
# P1-7 (#176) deliberately moved this OFF loopback: the platform gateway dials node_ip:port to
# splice a buyer into their rented VM, and a loopback-only bind is exactly why interactive VMs sat
# at "starting" and were auto-cancelled. So the seller-protection question is no longer "is it
# loopback" but "is the exposure the one the operator chose": all interfaces by default, narrowable
# to a single NIC with PB_TUNNEL_BIND, and nothing published at all for a batch template.
#
# NOTE the residual, deliberately not asserted away: on a node with a public IP this port is
# reachable from the internet, not only from the gateway. Narrowing that needs a firewall rule to
# the gateway address or an frp reverse tunnel (tracked as the P1-7 hardening follow-up).
#
# CALLED, not grepped — this module imports the real agent, so the exact docker argv is checked.
_saved_bind = os.environ.pop("PB_TUNNEL_BIND", None)
_pf_default = tf._publish_flags(8000)
os.environ["PB_TUNNEL_BIND"] = "10.1.2.3"
_pf_pinned = tf._publish_flags(8000)
if _saved_bind is None:
    os.environ.pop("PB_TUNNEL_BIND", None)
else:
    os.environ["PB_TUNNEL_BIND"] = _saved_bind
ok("a serving template publishes only on loopback for the authenticated tunnel",
   _pf_default == ["-p", "127.0.0.1:8000:8000"])
ok("legacy PB_TUNNEL_BIND cannot expose another NIC",
   _pf_pinned == ["-p", "127.0.0.1:8000:8000"])
ok("a BATCH template (no port) publishes nothing at all",
   tf._publish_flags(0) == [] and tf._publish_flags(None) == [])

# ---------------------------------------------------------------- ephemeral host port (one VM/node -> N)
# A second interactive VM on one node used to fail: the container port was bound on the host
# verbatim (0.0.0.0:8888:8888), so #2 collided. An ephemeral host port removes that limit.
ok("host_port maps an EPHEMERAL host port to the container port",
   tf._publish_flags(8888, 49213) == ["-p", "127.0.0.1:49213:8888"])
ok("no host_port keeps the old host==container behavior (backward compatible)",
   tf._publish_flags(8888) == ["-p", "127.0.0.1:8888:8888"])
ok("PB_TUNNEL_BIND cannot widen the ephemeral binding",
   (os.environ.__setitem__("PB_TUNNEL_BIND", "10.9.9.9"),
    tf._publish_flags(8888, 49213) == ["-p", "127.0.0.1:49213:8888"],
    os.environ.pop("PB_TUNNEL_BIND", None))[1])
_hp = tf._free_host_port()
ok("_free_host_port returns a real, unprivileged TCP port", isinstance(_hp, int) and 1024 < _hp < 65536)

# ---------------------------------------------------------------- reverse tunnel (buyer-IP privacy)
# With PB_TUNNEL_GATEWAY set, the container binds LOOPBACK and the node dials OUT (ssh -R) so it
# opens no inbound port; the gateway reaches the VM at 127.0.0.1:<remoteport>.
ok("reverse-tunnel bind is 127.0.0.1 (nothing public on the node)",
   tf._publish_flags(8888, 49213, bind="127.0.0.1") == ["-p", "127.0.0.1:49213:8888"])
_saved_gw = tf._TUN_GW
tf._TUN_GW = ""
ok("reverse tunnel OFF by default (public-IP path unchanged)", tf._reverse_tunnel_enabled() is False)
tf._TUN_GW = "pbtun@gw.example"
# The gateway address is only half of it. A node with no key cannot publish anything, so it must
# decline the rental UP FRONT rather than accept it, launch the container, and fail the buyer two
# and a half minutes later when every gateway port turns out to be equally unreachable.
_saved_key = tf._TUN_KEY
tf._TUN_KEY = "/nonexistent/tunnel_key"
ok("a gateway with NO tunnel key is not reverse-tunnel capable",
   tf._reverse_tunnel_enabled() is False)
tf._TUN_KEY = os.path.abspath(__file__)          # any file that exists == the key is installed
ok("reverse tunnel ON when PB_TUNNEL_GATEWAY is set", tf._reverse_tunnel_enabled() is True)
tf._TUN_PORTS = "20000-20002"


class _FakeProc:
    # `err` is what ssh printed before dying. Only a refused LISTEN PORT means another port is
    # worth trying; a rejected key or an unreachable gateway fails identically on all of them.
    def __init__(self, alive=True,
                 err="Warning: remote port forwarding failed for listen port 20000"):
        self._alive, self._err = alive, err
    def poll(self): return None if self._alive else 1
    def communicate(self, timeout=None): return ("", self._err)
    def terminate(self): self._alive = False


_spawned = []
_saved_popen, _saved_time = tf._popen, tf.time
tf.time = types.SimpleNamespace(sleep=lambda *_: None, time=__import__("time").time)
# first candidate port's ssh dies (port busy on gateway) -> should advance to the next and succeed
_seq = [_FakeProc(alive=False), _FakeProc(alive=True)]
def _fake_popen(cmd, capture_stderr=False):
    _spawned.append(cmd)
    return _seq[len(_spawned) - 1]
tf._popen = _fake_popen
tf._tunnels.clear()
rp = tf._open_reverse_tunnel(8888, 77)
ok("reverse tunnel skips a busy gateway port and binds the next", rp == 20001)
ok("the ssh -R maps a gateway loopback port to the container's loopback port",
   any("-R" in c and "127.0.0.1:20001:127.0.0.1:8888" in c for c in _spawned))
ok("the ssh command dials the configured gateway account and uses the tunnel key",
   _spawned[-1][-1] == "pbtun@gw.example" and tf._TUN_KEY in _spawned[-1])
ok("the live tunnel is tracked by task id", 77 in tf._tunnels and tf._tunnels[77][0] == 20001)
tf._kill_reverse_tunnel(77)
ok("teardown kills the tunnel and forgets it", 77 not in tf._tunnels)
tf._popen, tf.time, tf._TUN_GW = _saved_popen, _saved_time, _saved_gw
tf._TUN_KEY = _saved_key

# ---------------------------------------------------------------- server-driven orphan reap (P-2 leak)
# The exit-watchdog only reaps containers THIS process launched, on exit. A container from a prior
# agent run, or one whose rental the SERVER stopped while it keeps running, was never GC'd and kept
# holding its host port (this is what blocked every new jupyter VM on prod). The heartbeat now
# reports the node's live VM task ids; anything pb.kind=template not in that set is reaped after a
# grace period. Driven with a fake clock + stubbed docker so no daemon is needed.
class _Clock:
    def __init__(self): self.t = 1000.0
    def time(self): return self.t
_clk = _Clock(); _orig_time = tf.time; tf.time = _clk
tf._REAP_GRACE_S = 5
tf._orphan_since.clear()
_orig_list, _orig_label, _orig_clean = (
    tf._list_template_containers, tf._container_label_task, tf._cleanup_job_resources)
tf._list_template_containers = lambda: ["cA", "cB"]
tf._container_label_task = lambda cid: {"cA": "100", "cB": "200"}[cid]
_reaped = []
tf._cleanup_job_resources = lambda tid, cid=None: _reaped.append((str(tid), cid))

tf._reap_orphan_templates(None)
ok("reap does NOTHING when the server sent no active set (missing data != reap everything)", _reaped == [])
tf._reap_orphan_templates({"100"})          # cB(200) is now an orphan candidate, still within grace
ok("an orphan is NOT reaped within the grace window", _reaped == [])
_clk.t += 6                                  # past _REAP_GRACE_S
tf._reap_orphan_templates({"100"})
ok("an orphan past the grace window IS reaped (freeing its host port)", _reaped == [("200", "cB")])
ok("a container still in the active set is never reaped", ("100", "cA") not in _reaped)
# a rental that comes back into the active set clears its grace timer (no eventual false reap)
tf._orphan_since.clear(); _reaped.clear(); _clk.t = 2000.0
tf._reap_orphan_templates({"100", "200"})    # both live now
_clk.t += 100
tf._reap_orphan_templates({"100", "200"})
ok("a live rental never accumulates a grace timer (both stay)", _reaped == [])
tf.time = _orig_time
tf._list_template_containers, tf._container_label_task, tf._cleanup_job_resources = (
    _orig_list, _orig_label, _orig_clean)      # restore real fns (later tests inspect their source)

# ---------------------------------------------------------------- the reap must not touch BATCH
# The server's active set comes from active_vm_task_ids, which joins through VMRoute — so it lists
# INTERACTIVE rentals only. Selecting containers by pb.kind=template therefore offered every BATCH
# template job to the reap as an orphan it could never vouch for, and force-removed a job someone
# is paying for once the grace window passed. The selector has to match what the server can
# actually confirm.
import subprocess as _sp_mod   # noqa: E402


def _exec_fn(source: str, name: str):
    """Pull one function out of the OTHER runner's source and return it callable."""
    import re as _re2
    _pat = "def " + name + r"\(.*?(?=" + chr(10) + r"def )"
    m = _re2.search(_pat, source, _re2.S)
    ns = {"os": _os}
    exec(m.group(0), ns)
    return ns[name]


def _filter_used_by(mod):
    """Run the real _list_template_containers with docker stubbed, and report the label filter."""
    seen = {}

    class _R:
        stdout = ""

    _real = mod.subprocess.run if hasattr(mod, "subprocess") else _sp_mod.run
    _orig = _sp_mod.run
    _sp_mod.run = lambda cmd, **kw: (seen.update(cmd=cmd), _R())[1]
    try:
        mod._list_template_containers()
    finally:
        _sp_mod.run = _orig
    cmd = seen.get("cmd", [])
    return cmd[cmd.index("--filter") + 1] if "--filter" in cmd else ""


ok("the agent reaps by pb.interactive=1, not by pb.kind=template",
   _filter_used_by(tf) == "label=pb.interactive=1")

# ...and the rule that decides the label is EXECUTED, for both runners — a source grep would pass
# just as happily if every template were labelled interactive, which is the bug it guards.
_desk_src = open(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                               "..", "desktop-app", "task_fetcher.py"), encoding="utf-8").read()
for _who, _fn in (("agent", tf._interactive_labels),
                  ("desktop app", _exec_fn(_desk_src, "_interactive_labels"))):
    ok(f"{_who}: an interactive rental (vm_id set) IS labelled",
       _fn({"task_id": 1, "vm_id": "vm_abc"}) == ["--label", "pb.interactive=1"])
    ok(f"{_who}: a BATCH template job is NOT labelled, so the reap can never see it",
       _fn({"task_id": 2}) == [] and _fn({"task_id": 3, "vm_id": ""}) == [])

# and the launch really routes through that rule (not a second, divergent copy inline)
ok("the launch applies the shared rule rather than an inline copy",
   "_interactive_labels(task)" in inspect.getsource(tf._run_template)
   and "_interactive_labels(task)" in _desk_src)


def _publishes_via_helper(source: str) -> bool:
    """True when _run_template really EXECUTES `cmd += _publish_flags(...)`.

    Parsed, not grepped, and scoped to that one function body: a substring search is satisfied by a
    comment, a docstring or an unrelated line elsewhere in the file, so it would keep passing while
    the command actually built hard-codes a bind or drops the publish entirely.
    """
    import ast as _ast
    fn = next((n for n in _ast.walk(_ast.parse(source))
               if isinstance(n, _ast.FunctionDef) and n.name == "_run_template"), None)
    if fn is None:
        return False
    for node in _ast.walk(fn):
        if (isinstance(node, _ast.AugAssign) and isinstance(node.target, _ast.Name)
                and node.target.id == "cmd" and isinstance(node.value, _ast.Call)
                and getattr(node.value.func, "id", None) == "_publish_flags"):
            return True
    return False


ok("_run_template BUILDS its docker command from that helper (executable code, not a comment)",
   _publishes_via_helper(open(tf.__file__).read()))
# STD-C: the media runners chmod their bind-mounted output dir world-writable so a forced non-root
# container (AGENT_CONTAINER_USER) can still write results (mirrors notebook.py's workdir chmod).
for runner in ("_run_render", "_run_transcode", "_run_stitch"):
    ok(f"{runner} makes its bind-mounted output dir writable for a non-root container",
       "0o777" in src(runner))


# ---------------------------------------------------------------- desktop agent parity
desk = open(os.path.join(ROOT, "desktop-app", "task_fetcher.py")).read()
# The desktop agent carries its OWN copy of this logic, so it is executed too rather than trusted
# to match: a desktop node publishing on the wrong interface is exactly as unreachable, or as
# exposed, as a headless one.
_dns = {"os": os}
exec(__import__("re").search(r"def _publish_flags\(port[^)]*\):.*?(?=\ndef )", desk,
                             __import__("re").S).group(0), _dns)
_desk_publish = _dns["_publish_flags"]
_saved_bind = os.environ.pop("PB_TUNNEL_BIND", None)
_desk_default = _desk_publish(8000)
os.environ["PB_TUNNEL_BIND"] = "10.1.2.3"
_desk_pinned = _desk_publish(8000)
if _saved_bind is None:
    os.environ.pop("PB_TUNNEL_BIND", None)
else:
    os.environ["PB_TUNNEL_BIND"] = _saved_bind
# Every case the headless agent is held to. This is a SEPARATE implementation, so testing only its
# default would let it drift into publishing on the wrong NIC, or exposing a batch port that should
# publish nothing at all, while this gate stayed green.
ok("desktop publishes only on loopback",
   _desk_default == ["-p", "127.0.0.1:8000:8000"])
ok("desktop ignores an unsafe PB_TUNNEL_BIND too",
   _desk_pinned == ["-p", "127.0.0.1:8000:8000"])
ok("desktop BATCH template (no port) publishes nothing at all",
   _desk_publish(0) == [] and _desk_publish(None) == [])
ok("desktop _run_template BUILDS its command from the helper (executable code, not a comment)",
   _publishes_via_helper(desk))
ok("desktop template applies isolation + egress flags",
   "_isolation_flags(task)" in desk and "_egress_flags(task)" in desk)
ok("desktop render/transcode/stitch apply isolation flags",
   desk.count("_isolation_flags(task)") >= 4)
ok("desktop defines the hardening helpers", "def _isolation_flags" in desk and "--cap-drop" in desk)


# ---------------------------------------------------------------- host egress firewall
inst = open(os.path.join(HERE, "install.sh")).read()
ok("install.sh installs a DOCKER-USER egress firewall", "DOCKER-USER" in inst and "PB-EGRESS" in inst)
ok("firewall DROPs the cloud metadata endpoint (169.254.0.0/16)",
   "169.254.0.0/16" in inst and "-j DROP" in inst)
ok("firewall DROPs the seller's private LAN (10/8 + 192.168/16)",
   "10.0.0.0/8" in inst and "192.168.0.0/16" in inst)
ok("egress lockdown is re-applied on boot (survives docker restart)",
   "petabyte-egress.service" in inst and "After=docker.service" in inst)


# ---------------------------------------------------------------- result binds to output bytes
sr = tf._signed_result(7, status="completed", result="s3://b/out.tar", content_hash="a" * 64)
ok("a completed result carries the content_hash INSIDE the signed proof",
   sr["proof"].get("content_hash") == "a" * 64 and "signature" in sr)
ok("content_hash is the sha256 of real output bytes (render/transcode/stitch pass it)",
   src("_run_render").count("hashlib.sha256(raw)") >= 1
   and "hashlib.sha256(raw)" in src("_run_transcode")
   and "hashlib.sha256(raw)" in src("_run_stitch"))
_desk = open(os.path.join(ROOT, "desktop-app", "task_fetcher.py")).read()
ok("desktop agent also binds results to real output bytes",
   "content_hash=hashlib.sha256(raw)" in _desk and _desk.count("hashlib.sha256(raw)") >= 3)


# ---------------------------------------------------------------- benchmark authenticity
ok("agent measures FP16 matmul TFLOPS on-device (not an env stub)",
   hasattr(tf, "_measure_fp16_tflops") and "tflops_fp16" in src("_run_benchmark"))
ok("agent runs the real Blender Open Data benchmark (workload-relevant)",
   hasattr(tf, "_measure_blender_score") and "benchmark-launcher-cli" in src("_measure_blender_score")
   and "blender_optix" in src("_run_benchmark"))
ok("benchmark scores go INSIDE the signed proof (attributable, not bare meta)",
   "**metrics" in src("_run_benchmark") and "crypto.sign_proof(proof)" in src("_run_benchmark"))
ok("agent answers the server's FRESH proof-of-work challenge (anti-fabrication/replay)",
   "bench_seed" in src("_run_benchmark") and "compute_test_hash" in src("_run_benchmark")
   and "challenge_hash" in src("_run_benchmark"))
ok("benchmark measurement never crashes the agent (guarded)",
   all("except Exception" in src(fn) and "return None" in src(fn)
       for fn in ("_measure_fp16_tflops", "_measure_blender_score")))


# ---------------------------------------------------------------- tenant isolation (audit fixes)
# CRITICAL: template cache/work dirs must be a PER-TASK volume, never a shared pb-cache-<template>
# that hands one buyer's data to the next tenant.
ok("template cache is a PER-TASK volume, not a shared pb-cache-<template>",
   "_task_volume(task)" in src("_run_template") and "pb-cache-{task" not in src("_run_template"))
ok("_task_volume name carries the task id (unique per rental)",
   tf._task_volume({"task_id": 42, "template": "jupyter"}) == "pb-vol-t42-jupyter"
   and "42" in tf._task_volume({"task_id": 42, "template": "jupyter"}))
ok("two different tasks get two different volumes (no cross-tenant reuse)",
   tf._task_volume({"task_id": 1, "template": "jupyter"})
   != tf._task_volume({"task_id": 2, "template": "jupyter"}))
ok("every job container is labelled with its task id (findable + GC-able)",
   'pb.task={tid}' in src("_run_template"))
ok("the per-task volume is created with a pb.task label so teardown finds only this rental",
   'volume", "create", "--label", f"pb.task=' in src("_run_template"))
# CO-TENANT isolation: a networked template runs on its OWN bridge, not the shared default bridge.
ok("a networked template attaches a per-job network (co-tenant isolation)",
   "_ensure_job_network(tid)" in src("_run_template") and hasattr(tf, "_ensure_job_network"))
ok("_ensure_job_network delegates to the verified firewall policy",
   "network_policy.ensure(tid)" in src("_ensure_job_network"))
# FAIL CLOSED: omitting --network is NOT "no network", it is the shared default bridge. If the
# per-job bridge cannot be created the rental must be refused, never silently downgraded.
_tpl_src = src("_run_template")
ok("a template whose per-job network cannot be created is REFUSED (no default-bridge fallback)",
   "if not net:" in _tpl_src and "template refused" in _tpl_src
   and _tpl_src.index("if not net:") < _tpl_src.index('cmd += ["--network", net]'))
ok("the refusal reports the job failed instead of launching on the shared bridge",
   '"status": "failed"' in _tpl_src.split("if not net:")[1].split("cmd +=")[0])
ok("a failed template launch GCs its labelled volume/network (watchdog never sees it)",
   _tpl_src.count("_cleanup_job_resources(tid") >= 2)
# TEARDOWN: the watchdog GCs the rental's container + volume + network when it exits.
ok("the watchdog tears down a finished rental's resources",
   "_cleanup_job_resources(tid" in src("_pb_vm_scan") and hasattr(tf, "_cleanup_job_resources"))
ok("teardown is scoped by the pb.task label (only this rental's resources)",
   'filter", f"label=pb.task=' in src("_cleanup_job_resources")
   and 'network", "rm", f"pb-net-t' in src("_cleanup_job_resources"))
# CLUSTER: --network host is only used with a REAL WireGuard interface, not just the env flag.
import wireguard as _wg  # noqa: E402
_wg_if_src = inspect.getsource(_wg.interface_up) + inspect.getsource(_wg.link_is_up)
ok("distributed execution is refused even with a working WireGuard interface",
   "_signed_result" in src("_run_distributed") and "failed" in src("_run_distributed")
   and "docker" not in src("_run_distributed"))
ok("wireguard.interface_up checks the interface actually exists (wg show + ip link show)",
   '"wg", "show"' in _wg_if_src and '"link", "show"' in _wg_if_src)
# EXISTS is not UP: `wg show wg0` exits 0 for a configured-but-DOWN interface, and a wg device
# reports `state UNKNOWN` even when healthy — so only the IFF_UP flag inside <...> can be trusted.
# A leftover down wg0 must NOT open the --network host gate (host LAN + cloud metadata).
_wg_orig_run, _wg_orig_which = _wg.subprocess.run, _wg.shutil.which


class _R:
    def __init__(self, rc=0, out=""):
        self.returncode, self.stdout, self.stderr = rc, out, ""


def _wg_fake(link_out, link_rc=0, wg_rc=0, detail_out="", have_wg=True):
    def _which(b):
        return None if (b == "wg" and not have_wg) else f"/usr/bin/{b}"

    def _run(argv, **kw):
        if argv[0] == "wg":
            return _R(wg_rc)
        if "-d" in argv:
            return _R(link_rc, detail_out)
        return _R(link_rc, link_out)
    _wg.shutil.which, _wg.subprocess.run = _which, _run


try:
    _wg_fake("2: wg0: <POINTOPOINT,NOARP,UP,LOWER_UP> mtu 1420 ...")
    ok("an UP wireguard interface is accepted", _wg.interface_up("wg0") is True)
    _wg_fake("2: wg0: <POINTOPOINT,NOARP> mtu 1420 state DOWN ...")
    ok("an EXISTING but administratively DOWN wg0 is refused (no --network host without a mesh)",
       _wg.interface_up("wg0") is False)
    _wg_fake("2: wg0: <BROADCAST,MULTICAST,LOWER_UP> mtu 1500 ...")
    ok("LOWER_UP alone is not IFF_UP (flag is matched as a whole token)",
       _wg.interface_up("wg0") is False)
    _wg_fake("", link_rc=1)
    ok("a missing interface is refused", _wg.interface_up("wg0") is False)
    _wg_fake("2: wg0: <POINTOPOINT,NOARP,UP,LOWER_UP> mtu 1420 ...", wg_rc=1)
    ok("an UP interface that `wg show` rejects is refused", _wg.interface_up("wg0") is False)
    # Without wireguard-tools we ask the kernel for the link type: a dummy/bridge named wg0 must
    # not pass as a mesh just because someone named it wg0 and brought it up.
    _wg_fake("2: wg0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 ...",
             detail_out="2: wg0: <UP> ... link/ether dummy addrgenmode eui64", have_wg=False)
    ok("without `wg`, a non-wireguard link named wg0 is refused", _wg.interface_up("wg0") is False)
    _wg_fake("2: wg0: <POINTOPOINT,NOARP,UP,LOWER_UP> mtu 1420 ...",
             detail_out="2: wg0: <UP> ... wireguard addrgenmode none", have_wg=False)
    ok("without `wg`, a real UP wireguard link is accepted", _wg.interface_up("wg0") is True)
finally:
    _wg.subprocess.run, _wg.shutil.which = _wg_orig_run, _wg_orig_which

# ---------------------------------------------------------------- co-tenant isolation (desktop)
ok("desktop template uses a PER-TASK volume too (no shared pb-cache-)",
   "pb-vol-t{tid}-" in _desk and "pb-cache-{task" not in _desk)
ok("desktop networking must use the shared local Linux firewall policy",
   "network_policy.ensure(tid)" in _desk and "_ensure_job_network(tid)" in _desk)
ok("desktop also FAILS CLOSED when the per-job bridge can't be made (no default-bridge fallback)",
   "if not net:" in _desk and "template refused" in _desk)
# TEARDOWN (desktop): the fork launches DETACHED containers and had no reaper at all, so a
# finished rental's volume (buyer work dir / HF token / game saves) lived on for the next tenant.
ok("desktop tears down a rental's container+volume+network, scoped by the pb.task label",
   "def _cleanup_job_resources(" in _desk and 'filter", f"label=pb.task=' in _desk
   and 'network", "rm", f"pb-net-t' in _desk)
ok("desktop reaps a detached template once its container exits (watcher thread)",
   "def _watch_template_exit(" in _desk and "_watch_template_exit(tid)" in _desk)
ok("desktop reaps a stale volume for this task id BEFORE launching (no remount of old data)",
   "_cleanup_job_resources(tid, kill_running=False)" in _desk)
ok("the pre-launch sweep never force-removes a container that is still running",
   "status=exited" in _desk and "if kill_running:" in _desk)
ok("desktop cleans up after a FAILED launch too (volume/network don't leak)",
   "_cleanup_job_resources(tid, name)" in _desk)
ok("the exit watcher only reaps on a SUCCESSFUL, empty `docker ps` (a daemon hiccup keeps polling)",
   "if r.returncode != 0:" in _desk and "continue" in _desk)

# ---------------------------------------------------------------- host-service protection (install.sh)
ok("install.sh blocks a container from opening NEW connections to the host (bridge INPUT drop)",
   "PB-HOST-IN" in inst and "ctstate NEW -j DROP" in inst and "-i br+" in inst)
ok("install.sh blocks outbound spam/worm ports so a job can't burn the seller's IP reputation",
   "PETABYTE_BLOCK_ABUSE_PORTS" in inst and '--dport "$_p"' in inst
   and "25 465 587" in inst and "445" in inst)

# ---------------------------------------------------------------- peer resource-abuse caps (DoS a co-tenant/host)
ok("every buyer container caps /dev/shm (--shm-size) so it can't exhaust host memory via shm",
   '"--shm-size"' in src("_isolation_flags"))
ok("every buyer container caps file descriptors and processes (--ulimit nofile/nproc)",
   "nofile=" in src("_isolation_flags") and "nproc=" in src("_isolation_flags"))
ok("an operator can bound the job's disk (AGENT_JOB_DISK_GB -> --storage-opt size=)",
   "AGENT_JOB_DISK_GB" in src("_isolation_flags") and "storage-opt" in src("_isolation_flags"))
_iso = tf._isolation_flags({})
ok("the shm/ulimit caps are ON by default (no task sizing needed)",
   "--shm-size" in _iso and any(f.startswith("nofile=") for f in _iso)
   and any(f.startswith("nproc=") for f in _iso))
ok("disk quota is OPT-IN (not added unless AGENT_JOB_DISK_GB is set — errors on ext4 otherwise)",
   "--storage-opt" not in _iso)

# ---------------------------------------------------------------- cross-tenant VRAM residue (P-2)
ok("a GPU job wipes free VRAM BEFORE it runs (prev tenant's data can't be read)",
   "_wipe_gpu_vram(task.get(\"task_id\"))" in src("job_loop") and hasattr(tf, "_wipe_gpu_vram"))
_wipe_src = inspect.getsource(tf._wipe_gpu_vram)
ok("VRAM wipe tries nvidia-smi --gpu-reset then a cached CUDA memset image",
   "--gpu-reset" in _wipe_src and "_cuda_wipe_image" in _wipe_src)
ok("the VRAM memset overwrites free device memory with zeros",
   "torch.zeros" in tf._VRAM_WIPE_PY and "mem_get_info" in tf._VRAM_WIPE_PY)
ok("VRAM wipe only uses a LOCALLY-CACHED image (no multi-GB pull on the hot path)",
   '"image", "inspect"' in inspect.getsource(tf._cuda_wipe_image))
ok("VRAM wipe never raises (best-effort, time-bounded)",
   "except Exception" in _wipe_src and "VRAM_WIPE_TIMEOUT_S" in _wipe_src)

# ---------------------------------------------------------------- download MITM: DNS pinning (P-3)
ok("a networked job pins its DNS resolver (host can't trivially DNS-lie to MITM pip/model pulls)",
   hasattr(tf, "_dns_flags") and tf._dns_flags()[:1] == ["--dns"])
ok("networked templates use explicit DNS; batch containers have no network",
   "_dns_flags()" in src("_run_template") and '"--network", "none"' in src("build_container_cmd"))
ok("DNS pinning is operator-tunable (AGENT_JOB_DNS)",
   "AGENT_JOB_DNS" in inspect.getsource(tf._dns_flags))

# ---------------------------------------------------------------- seller LAN protection (P-8)
ok("install.sh drops the host's REAL LAN subnets (covers a 172.16/12 corporate LAN)",
   "ip -o -4 addr show" in inst and "172.1[6-9]." in inst and "-d \"$_cidr\" -j DROP" in inst)

# ---------------------------------------------------------------- GPU identity (provision)
import provision as _prov  # noqa: E402
ok("provision verifies a declared GPU against nvidia-smi before registering",
   hasattr(_prov, "verify_declared_gpu") and "verify_declared_gpu(gpu, gc)" in open(
       os.path.join(ROOT, "lumaris_agent", "provision.py")).read())
_gpu_fails = {"n": 0}
_orig_names = _prov.nvidia_gpu_names
_orig_rows = _prov.nvidia_gpu_rows
_orig_fail = _prov._fail
try:
    _prov.nvidia_gpu_names = lambda: []                      # pretend: no NVIDIA GPU on this box
    def _boom(*a, **k):
        _gpu_fails["n"] += 1
        raise SystemExit(1)
    _prov._fail = _boom
    try:
        _prov.verify_declared_gpu("NVIDIA H100 80GB HBM3", 1)  # the env-var lie
    except SystemExit:
        pass
    ok("declaring an H100 on a GPU-less box is refused", _gpu_fails["n"] == 1)
    _gpu_fails["n"] = 0
    _prov.verify_declared_gpu(None, 0)                        # a genuine CPU-only node
    ok("a genuine CPU-only node (no GPU claimed) registers fine", _gpu_fails["n"] == 0)
    _prov.nvidia_gpu_names = lambda: ["NVIDIA GeForce RTX 4090"]
    _prov.verify_declared_gpu("NVIDIA GeForce RTX 4090", 1)   # a real GPU that nvidia-smi shows
    ok("a real GPU that nvidia-smi confirms registers fine", _gpu_fails["n"] == 0)
    _gpu_fails["n"] = 0
    try:
        _prov.verify_declared_gpu("NVIDIA GeForce RTX 4090", 4)   # claims 4, has 1
    except SystemExit:
        pass
    ok("declaring more GPUs than present is refused", _gpu_fails["n"] == 1)

    # The count alone was never the lie that pays: one cheap card + the NAME of an expensive one
    # priced a 3060 as an H100. The declared MODEL must match what nvidia-smi shows.
    _gpu_fails["n"] = 0
    _prov.nvidia_gpu_names = lambda: ["NVIDIA GeForce RTX 3060"]
    try:
        _prov.verify_declared_gpu("NVIDIA H100 80GB HBM3", 1)
    except SystemExit:
        pass
    ok("declaring an H100 on a box that has an RTX 3060 is refused (model, not just count)",
       _gpu_fails["n"] == 1)
    _gpu_fails["n"] = 0
    _prov.nvidia_gpu_names = lambda: ["NVIDIA GeForce RTX 4090"]
    _prov.verify_declared_gpu("RTX 4090", 1)          # the short name a seller actually types
    ok("a legitimate short model name still registers ('RTX 4090' vs nvidia-smi's long string)",
       _gpu_fails["n"] == 0)
    _gpu_fails["n"] = 0
    try:
        _prov.verify_declared_gpu("NVIDIA", 1)        # normalises to nothing -> must not match all
    except SystemExit:
        pass
    ok("a vendor-only GPU_MODEL ('NVIDIA') does not match every card on the host",
       _gpu_fails["n"] == 1)

    # MIXED host: 1x H100 + 3x GT710 must never be listed as "4x H100" — only the cards that
    # actually match the declared model count toward GPU_COUNT.
    _gpu_fails["n"] = 0
    _prov.nvidia_gpu_names = lambda: ["NVIDIA H100 80GB HBM3", "NVIDIA GeForce GT 710",
                                      "NVIDIA GeForce GT 710", "NVIDIA GeForce GT 710"]
    try:
        _prov.verify_declared_gpu("NVIDIA H100 80GB HBM3", 4)
    except SystemExit:
        pass
    ok("a mixed host cannot be listed as 4x the best card it holds", _gpu_fails["n"] == 1)
    _gpu_fails["n"] = 0
    _prov.verify_declared_gpu("NVIDIA H100 80GB HBM3", 1)     # the honest 1x H100 listing
    ok("the honest subset of a mixed host still registers", _gpu_fails["n"] == 0)

    # HALF-declaration: GPU_COUNT with no GPU_MODEL used to return early as "CPU-only" and still
    # reach the registry as an N-GPU spec.
    _gpu_fails["n"] = 0
    try:
        _prov.verify_declared_gpu(None, 8)
    except SystemExit:
        pass
    ok("GPU_COUNT with no GPU_MODEL is refused, not treated as CPU-only", _gpu_fails["n"] == 1)
    _gpu_fails["n"] = 0
    try:
        _prov.verify_declared_gpu("NVIDIA H100", 0)
    except SystemExit:
        pass
    ok("GPU_MODEL with a zero count is refused (nothing can be scheduled onto it)",
       _gpu_fails["n"] == 1)

    # ALLOW_UNVERIFIED_GPU is a CI/dev affordance and is exactly the switch a lying seller flips.
    # It must do nothing unless the box is explicitly marked non-production; unset == production.
    _prov.nvidia_gpu_names = lambda: []
    for _k in ("ALLOW_UNVERIFIED_GPU", "ENVIRONMENT", "DEPLOYMENT_ENVIRONMENT"):
        os.environ.pop(_k, None)
    os.environ["ALLOW_UNVERIFIED_GPU"] = "true"
    _gpu_fails["n"] = 0
    try:
        _prov.verify_declared_gpu("NVIDIA H100 80GB HBM3", 8)
    except SystemExit:
        pass
    ok("ALLOW_UNVERIFIED_GPU does NOT bypass the check with ENVIRONMENT unset (prod fail-closed)",
       _gpu_fails["n"] == 1)
    os.environ["ENVIRONMENT"] = "production"
    _gpu_fails["n"] = 0
    try:
        _prov.verify_declared_gpu("NVIDIA H100 80GB HBM3", 8)
    except SystemExit:
        pass
    ok("ALLOW_UNVERIFIED_GPU is rejected in production", _gpu_fails["n"] == 1)
    os.environ["ENVIRONMENT"] = "test"
    _gpu_fails["n"] = 0
    _prov.verify_declared_gpu("NVIDIA H100 80GB HBM3", 8)
    ok("ALLOW_UNVERIFIED_GPU still works for a CI/dev harness (ENVIRONMENT=test)",
       _gpu_fails["n"] == 0)

    # detect(): the mixed-host honesty fix lives in auto-detection too.
    _prov.nvidia_gpu_rows = lambda: [("NVIDIA H100 80GB HBM3", "81559"),
                                     ("NVIDIA GeForce GT 710", "2048"),
                                     ("NVIDIA GeForce GT 710", "2048")]
    for _k in ("GPU_MODEL", "GPU_COUNT", "VRAM_GB"):
        os.environ.pop(_k, None)
    _cpu, _ram, _m, _c, _v = _prov.detect()
    ok("auto-detect on a mixed host reports one homogeneous group, not 3x the best card",
       _m == "NVIDIA GeForce GT 710" and _c == 2 and _v == 2)
finally:
    for _k in ("ALLOW_UNVERIFIED_GPU", "ENVIRONMENT", "DEPLOYMENT_ENVIRONMENT"):
        os.environ.pop(_k, None)
    _prov.nvidia_gpu_names = _orig_names
    _prov.nvidia_gpu_rows = _orig_rows
    _prov._fail = _orig_fail


# ---- a host whose GPU containers can't run CUDA stops being offered (2026-09-23, 5070 Ti) ----
_saved = (tf._wipe_gpu_vram, tf._cuda_wipe_image, tf.time, tf.threading.Thread, tf._sched.copy())
_threads, _wipes = [], []
try:
    tf._sched.update(start=None, end=None)                 # always-on selling window
    tf.threading.Thread = lambda target, **k: types.SimpleNamespace(start=lambda: _threads.append(target))
    tf._GPU_UNUSABLE.clear()
    ok("healthy node is offered", tf._selling_now())
    tf._mark_gpu_unusable("test")
    ok("after a failed wipe the node is NOT offered (heartbeat selling_now=False)", not tf._selling_now())
    ok("and refuses new jobs itself", not tf._job_within_schedule({}))
    ok("and queues the support diagnostics report (after the re-test loop)", len(_threads) == 2)
    _orig_pending = tf._fixes.pending
    tf._GPU_UNUSABLE.clear()
    tf._fixes.pending = lambda: True
    ok("a pending support fix takes the node off sale (heartbeat selling_now=False)", not tf._selling_now())
    tf._fixes.pending = _orig_pending
    tf._GPU_UNUSABLE.set()
    tf.time = types.SimpleNamespace(sleep=lambda *_: None, time=__import__("time").time)
    tf._cuda_wipe_image = lambda: "pytorch/pytorch:2.7.0-cuda12.8-cudnn9-runtime"
    with tf._pb_vm_lock:
        tf._pb_vm_watch["t1"] = {"name": "live", "reported": False}
    _res = iter([True])
    tf._wipe_gpu_vram = lambda *a: (_wipes.append(1), next(_res))[1]
    _live_rounds = iter([True, False])                     # round 1: a buyer is live -> skip
    _orig_live = tf._rental_live
    tf._rental_live = lambda: next(_live_rounds)
    _threads[0]()                                          # run the re-test loop synchronously
    ok("re-test never wipes under a live rental, then re-offers once a wipe passes",
       len(_wipes) == 1 and tf._selling_now())
    _orig_has_gpu = tf.gpu_runtime.has_gpu
    tf.gpu_runtime.has_gpu = lambda: True
    tf._wipe_gpu_vram = lambda *a: _wipes.append("startup") or False
    os.environ.pop("AGENT_ALLOW_UNVERIFIED_VRAM", None)
    tf._rental_live = lambda: True
    tf._gpu_startup_selftest()
    ok("startup self-test skips while a restored rental owns the GPU",
       "startup" not in _wipes and tf._selling_now())
    tf._rental_live = lambda: False
    tf._gpu_startup_selftest()
    ok("idle startup self-test that fails delists the node before the first heartbeat",
       "startup" in _wipes and not tf._selling_now())
    tf.gpu_runtime.has_gpu = _orig_has_gpu
    tf._rental_live = _orig_live
finally:
    with tf._pb_vm_lock:
        tf._pb_vm_watch.pop("t1", None)
    tf._wipe_gpu_vram, tf._cuda_wipe_image, tf.time, tf.threading.Thread = _saved[:4]
    tf._sched.clear(); tf._sched.update(_saved[4])
    tf._GPU_UNUSABLE.clear()

print(f"\n=== sandbox: {'0 failures' if _fail == 0 else str(_fail) + ' FAILED'} ===")
raise SystemExit(1 if _fail else 0)
