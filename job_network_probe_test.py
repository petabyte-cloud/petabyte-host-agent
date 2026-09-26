"""job_network_probe_test.py -- a node that can't isolate a networked app says so, and says why.

Prod 2026-09-24/25: spec 45 (RTX 5070 Ti Laptop) refused every llama.cpp launch in ~5 s with a
generic "could not create the per-job network". network_policy._local_daemon() had raised a precise
reason that _ensure_job_network swallowed, the buyer was refunded, the node was quarantined, and the
seller never learned why. Batch (--network none) jobs kept passing, so the node looked healthy.

  1. network_policy.probe() -> (ok, reason) without creating any network or rule, + a seller hint;
  2. a refused launch carries the reason into the task log, the agent UI state and the heartbeat;
  3. the heartbeat reports {ok, reason, hint} once probed (omitted before: old-API safe).

Offline: subprocess, sys.platform and euid are stubbed. Run: python job_network_probe_test.py
"""
import inspect
import json
import os
import subprocess
import sys
import types
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("PETABYTE_API_URL", "http://localhost")
os.environ.setdefault("PETABYTE_API_KEY", "test")
os.environ.setdefault("PETABYTE_SPEC_ID", "1")
import network_policy as np

_fail = 0


def ok(label, cond, extra=""):
    global _fail
    print(("ok   " if cond else "FAIL ") + label + (f"   [{extra}]" if extra and not cond else ""))
    if not cond:
        _fail += 1


def fake_docker(endpoint="unix:///var/run/docker.sock", name="node1", ostype="linux", rc=0):
    calls = []

    def run(args, **kw):
        calls.append(list(args))
        if args[:2] == ["docker", "context"]:
            return subprocess.CompletedProcess(args, rc, endpoint + "\n", "")
        if args[:2] == ["docker", "info"]:
            return subprocess.CompletedProcess(args, rc, json.dumps({"Name": name, "OSType": ostype}), "")
        raise AssertionError(f"probe must not run {args}")   # never creates a network or a rule
    return run, calls


def probe(platform="linux", euid=0, docker=None, which=lambda t: "/usr/sbin/" + t, env=None):
    run, calls = docker or fake_docker()
    with patch.object(np.sys, "platform", platform), \
         patch.object(np.os, "geteuid", lambda: euid, create=True), \
         patch.object(np.subprocess, "run", run), \
         patch.object(np.shutil, "which", which), \
         patch.object(np.socket, "gethostname", lambda: "node1"), \
         patch.dict(os.environ, env or {}, clear=False):
        if not env:
            os.environ.pop("DOCKER_HOST", None)
        return np.probe(), calls


# ------------------------------------------------------------------ 1. the probe
(r, calls) = probe()
ok("root + native local engine + iptables/ip present -> (True, None)", r == (True, None), str(r))
ok("the probe only asks docker (context + info): no network, no iptables rule", all(c[0] == "docker" for c in calls))

(r, _) = probe(platform="darwin")
ok("macOS/Windows -> 'a local Linux firewall is required'", r == (False, "a local Linux firewall is required"), str(r))
(r, _) = probe(euid=1000)
ok("non-root (rootless) -> 'a local Linux firewall is required'", r[1] == "a local Linux firewall is required", str(r))
(r, _) = probe(docker=fake_docker(endpoint="unix:///home/u/.docker/desktop/docker.sock"))
ok("Docker Desktop context -> 'remote container daemon refused'", r == (False, "remote container daemon refused"), str(r))
(r, _) = probe(env={"DOCKER_HOST": "tcp://10.0.0.5:2375"})
ok("DOCKER_HOST set -> 'remote container daemon refused'", r[1] == "remote container daemon refused", str(r))
(r, _) = probe(docker=fake_docker(name="docker-desktop"))
ok("Docker Desktop's daemon (its WSL integration) -> named as such", r == (False, np.DESKTOP), str(r))
ok("…and its hint sends a Windows seller to the installer (own WSL distro), Docker Desktop untouched",
   "re-run the Petabyte installer" in np.hint(r[1]) and "leaves Docker Desktop as it is" in np.hint(r[1]))
(r, _) = probe(docker=fake_docker(name="elsewhere"))
ok("daemon on another host -> 'container daemon and firewall must share the host'",
   r == (False, "container daemon and firewall must share the host"), str(r))
(r, _) = probe(which=lambda t: None if t == "ip6tables" else "/usr/sbin/" + t)
ok("missing ip6tables -> named", r == (False, "missing ip6tables"), str(r))
(r, _) = probe(docker=fake_docker(rc=1))
ok("a failing docker command is named (not the generic 'unavailable')",
   r[0] is False and "`docker context inspect` failed" in r[1], str(r))


def no_docker(args, **kw):
    raise FileNotFoundError("docker")


(r, _) = probe(docker=(no_docker, []))
ok("docker not installed -> False with a reason, never an exception",
   r == (False, "container daemon unreachable (FileNotFoundError)"), str(r))

ok("hint: firewall/rootless -> root on Linux with the native Docker Engine",
   "root on Linux with the native Docker Engine" in np.hint("a local Linux firewall is required"))
ok("hint: remote daemon -> unset DOCKER_HOST / docker context use default",
   "DOCKER_HOST" in np.hint("remote container daemon refused")
   and "docker context use default" in np.hint("remote container daemon refused"))
ok("hint: missing tools -> install iptables and iproute2", "iproute2" in np.hint("missing iptables, ip"))
ok("hint: anything else -> the general root + native engine advice",
   "Docker Desktop" in np.hint("job firewall differs from required policy"))

# ------------------------------------------------------------------ 2. reason propagation in the agent
import task_fetcher as tf

ui = types.ModuleType("ui")
ui.agent_status = {}
sys.modules["ui"] = ui

with patch.object(np, "ensure", side_effect=np.NetworkUnavailable("remote container daemon refused")):
    net, why = tf._ensure_job_network(373)
ok("_ensure_job_network returns the reason instead of swallowing it",
   net is None and why == "remote container daemon refused", str((net, why)))
ok("…and marks this node batch-only for the next heartbeat",
   tf._JOB_NET["ok"] is False and tf._JOB_NET["reason"] == "remote container daemon refused")
ok("the seller's agent UI shows the reason and the hint",
   ui.agent_status.get("job_network", {}).get("reason") == "remote container daemon refused"
   and "DOCKER_HOST" in (ui.agent_status["job_network"].get("hint") or ""))
with patch.object(np, "ensure", return_value="pb-net-t374"):
    ok("a built network is returned with no reason", tf._ensure_job_network(374) == ("pb-net-t374", None))

_logs, _posts = [], []
tf.report_log = lambda tid, msg: _logs.append(msg)
tf._post = lambda path, payload: _posts.append((path, payload))
tf._cleanup_job_resources = lambda tid, name=None: None
tf._isolation_flags = lambda task: []
tf._reverse_tunnel_enabled = lambda: False
tf._restore_volume = lambda *a, **k: None
tf._start_backup_thread = lambda task: None
with patch.object(np, "ensure", side_effect=np.NetworkUnavailable("a local Linux firewall is required")), \
     patch("shutil.which", return_value="/usr/bin/docker"):
    tf._run_template({"task_id": 473, "template": "llamacpp", "image": "x", "egress": "limited",
                      "params": {}})
refusal = next((m for m in _logs if "template refused" in m), "")
ok("the refusal task-log line names the reason (task 473's line had none)",
   "a local Linux firewall is required" in refusal, refusal)
ok("…and still fails CLOSED: vm_details failed, no docker run on the default bridge",
   ("/jobs/vm_details", {"task_id": 473, "vm_type": "template", "vm_id": "", "status": "failed"}) in _posts)

# ------------------------------------------------------------------ 3. startup probe + heartbeat
import isolation

with patch.object(np, "probe", return_value=(True, None)), \
     patch.object(isolation, "last_repair", return_value={"repaired": True, "ok": True}):
    tf._probe_job_network()
ok("a passing probe clears the batch-only state, and reports the auto-repair",
   tf._JOB_NET == {"ok": True, "reason": None, "hint": None, "repaired": True}, str(tf._JOB_NET))
with patch.object(np, "probe", return_value=(False, "a local Linux firewall is required")):
    tf._probe_job_network()
ok("a failing probe records reason + hint",
   tf._JOB_NET["ok"] is False and "native Docker Engine" in tf._JOB_NET["hint"])
_hb_src = inspect.getsource(tf.heartbeat_loop)
ok("the heartbeat carries job_network once probed (omitted while unknown)",
   '_hb["job_network"] = dict(_JOB_NET)' in _hb_src and '_JOB_NET["ok"] is not None' in _hb_src)
import tempfile

_bf = os.path.join(tempfile.mkdtemp(), "bundle.sha256")
tf._BUNDLE_FILE = _bf
ok("no recorded bundle (git/dev install) -> nothing reported", tf._agent_bundle() is None)
with open(_bf, "w") as f:
    f.write("ab" * 32 + "\n")
ok("the bundle update.sh recorded is reported", tf._agent_bundle() == "ab" * 32)
with open(_bf, "w") as f:
    f.write("not-a-sha")
ok("anything that isn't a sha256 is not", tf._agent_bundle() is None)
ok("the heartbeat carries agent_bundle, re-read each beat",
   "_agent_bundle()" in _hb_src and '_hb["agent_bundle"]' in _hb_src)
_run_src = inspect.getsource(tf.run_agent)
ok("the agent probes at startup, before the first heartbeat, and re-probes in the background",
   _run_src.index("_probe_job_network()") < _run_src.index("target=heartbeat_loop")
   and "_job_network_loop" in _run_src)

print(f"\n{'ALL PASS' if _fail == 0 else f'{_fail} FAILED'}")
sys.exit(1 if _fail else 0)
