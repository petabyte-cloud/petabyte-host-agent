"""template_launch_test.py -- a template launch says WHY it failed, and Ollama gets its model.

1. A refused `docker run` used to be reported as a bare "container launch failed": Docker's stderr
   (pull denied, bad image, port taken) was thrown away, so nobody could tell why a node refused.
2. The ollama template passed the buyer's model as OLLAMA_MODEL, which the official image never
   reads: buyers got an empty server. The agent now pulls it into the running container.

Offline: docker is stubbed. Run: python template_launch_test.py
"""
import os
import subprocess
import sys
import threading
import types
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("PETABYTE_API_URL", "http://localhost")
os.environ.setdefault("PETABYTE_API_KEY", "test")
os.environ.setdefault("PETABYTE_SPEC_ID", "1")
import task_fetcher as tf  # noqa: E402

_fail = 0


def ok(label, cond):
    global _fail
    print(("ok   " if cond else "FAIL ") + label)
    if not cond:
        _fail += 1


_logs, _posts, _results = [], [], []
tf.report_log = lambda tid, msg: _logs.append(msg)
tf._post = lambda path, payload: _posts.append((path, payload))
tf._post_result_ack_retry = lambda payload, attempts=4: (_results.append(payload), True)[1]
tf._signed_result = lambda tid, status="completed", result=None, **k: {
    "task_id": tid, "status": status, "result": result}
tf._cleanup_job_resources = lambda tid, name=None: None
# Image admission/ownership has its own fake-Docker suite; keep this runner fixture offline.
tf.template_storage.prepare = lambda *a, **k: "cached"
tf._start_storage_guard = lambda *a: None
tf._isolation_flags = lambda task: []
tf._reverse_tunnel_enabled = lambda: False
tf._pb_vm_started["on"] = True                 # no background watchdog thread in a unit test

# ------------------------------------------------------------------ the failure reason
_err = subprocess.CalledProcessError(
    125, ["docker", "run"], "",
    "Unable to find image 'x:latest' locally\n"
    "docker: Error response from daemon: pull access denied for x, repository does not exist "
    "or may require 'docker login': token=abc123secret.\n")
_why = tf._launch_failure_reason(_err)
ok("a refused docker run reports Docker's own stderr", "pull access denied" in _why)
ok("with secrets masked", "abc123secret" not in _why and "[redacted]" in _why)
ok("any other exception names its type",
   tf._launch_failure_reason(ValueError("invalid container environment"))
   == "ValueError: invalid container environment")

# ------------------------------------------------------------------ _run_template carries it through
_task = {"task_id": 31, "template": "tpl", "image": "x:latest", "egress": "none", "params": {}}
with patch("shutil.which", return_value="/usr/bin/docker"), \
     patch("subprocess.run", return_value=Mock(returncode=125, stdout="",
                                              stderr="docker: Error response from daemon: "
                                                     "Conflict. The container name is in use.")):
    tf._run_template(dict(_task))
ok("the buyer-visible log says why the launch failed",
   any(m.startswith("container launch failed: docker: Error response from daemon: Conflict")
       for m in _logs))
ok("and so does the failed result",
   _results and _results[-1]["status"] == "failed"
   and "Conflict. The container name is in use." in _results[-1]["result"])

# ------------------------------------------------------------------ ollama pulls the requested model
_pulls = []
_real_start_pull = tf._start_ollama_pull
tf._start_ollama_pull = lambda tid, name, model: _pulls.append((tid, model))
with patch("shutil.which", return_value="/usr/bin/docker"), \
     patch("subprocess.run", return_value=Mock(returncode=0, stdout="cid123\n", stderr="")) as _run:
    tf._run_template(dict(_task, task_id=32, template="ollama", model_env="OLLAMA_MODEL",
                          params={"model": "llama3", "max_startup_seconds": 300}))
ok("launching the ollama template starts a pull of the buyer's model", _pulls == [(32, "llama3")])
ok("budgeted Ollama startup reports loading until the requested model is pulled",
   any(p[1].get("task_id") == 32 and p[1].get("status") == "loading" for p in _posts))
ok("the model is still passed as OLLAMA_MODEL (harmless, backward compatible)",
   "--env-file" in _run.call_args.args[0])

tf.time = types.SimpleNamespace(sleep=lambda s: None, time=tf.time.time)
with tf._pb_vm_lock:
    tf._pb_vm_watch[33] = {"name": "pb-ollama-1", "reported": False}
_logs.clear()
_posts.clear()
with patch("subprocess.run", side_effect=[
        Mock(returncode=1, stdout="", stderr="Error: could not connect to ollama app, is it running?"),
        Mock(returncode=0, stdout="success", stderr="")]) as _run:
    tf._ollama_pull(33, "pb-ollama-1", "llama3:8b")
ok("the pull runs inside the rental's container",
   _run.call_args.args[0] == ["docker", "exec", "pb-ollama-1", "ollama", "pull", "llama3:8b"])
ok("it waits for the server inside to come up, then succeeds", _run.call_count == 2)
ok("progress is logged for the buyer",
   any("pulling model llama3:8b" in m for m in _logs) and any("pulled" in m for m in _logs))
ok("'ready' is posted once the model is in",
   ("/jobs/vm_details", {"task_id": 33, "vm_type": "template", "vm_id": "", "status": "ready"}) in _posts)
ok("no 'loading' is posted (Ollama serves while pulling; 'loading' would be unbilled use)",
   not any(p[1].get("status") == "loading" for p in _posts))

_logs.clear()
_posts.clear()
with patch("subprocess.run", return_value=Mock(returncode=1, stdout="",
                                               stderr="Error: pull model manifest: file does not exist")):
    tf._ollama_pull(33, "pb-ollama-1", "nosuchmodel")
ok("an unknown model is reported, not retried, and never marked ready",
   any("file does not exist" in m for m in _logs) and not _posts)

with patch("subprocess.run") as _run:
    tf._ollama_pull(33, "pb-ollama-1", "--insecure")
ok("a model name that looks like a flag is refused before docker exec", not _run.called)

with tf._pb_vm_lock:
    tf._pb_vm_watch.pop(33)
with patch("subprocess.run") as _run:
    tf._ollama_pull(33, "pb-ollama-1", "llama3")
ok("nothing is pulled for a rental that already ended", not _run.called)

_ran_on = []
_done = threading.Event()
tf._ollama_pull = lambda tid, name, model: (_ran_on.append(threading.current_thread()), _done.set())
_real_start_pull(34, "pb-ollama-2", "llama3")
_done.wait(5)
ok("the pull runs off the job loop (its own thread), so other jobs are not blocked",
   _ran_on and _ran_on[0] is not threading.main_thread())

# ------------------------------------------------------------------ unattended startup script
_logs.clear()
_watched = threading.Event()
_real_watch = tf._watch_startup_script
tf._watch_startup_script = lambda tid, name, workdir: _watched.set() if tid == 35 else None
_script = "#!/bin/bash\necho TOPSECRET-token > out.txt\n"
with patch("shutil.which", return_value="/usr/bin/docker"), \
     patch("subprocess.run", return_value=Mock(returncode=0, stdout="cid35\n", stderr="")) as _run:
    tf._run_template(dict(_task, task_id=35, template="pytorch", cache="/home/jovyan/work",
                          params={"startup_script": _script}))
_execs = [c for c in _run.call_args_list if c.args and list(c.args[0][:2]) == ["docker", "exec"]]
ok("the script is written into the rental's own container over stdin",
   any(c.args[0][2] == "-i" and c.kwargs.get("input") == _script for c in _execs))
ok("then started detached inside that container, logging to the workspace's startup.log",
   any(c.args[0][2] == "-d" and "/home/jovyan/work" in c.args[0][-1]
       and "startup.log" in c.args[0][-1] and "startup.exitcode" in c.args[0][-1] for c in _execs))
ok("the script never appears in an argv or a log line",
   not any("TOPSECRET" in str(c.args) for c in _run.call_args_list)
   and not any("TOPSECRET" in m for m in _logs))
ok("the buyer's log says where the output goes", any("startup.log" in m for m in _logs))
ok("its exit is watched off the job loop", _watched.wait(5))
tf._watch_startup_script = _real_watch

with tf._pb_vm_lock:
    tf._pb_vm_watch[36] = {"name": "pb-pytorch-1", "reported": False}
_logs.clear()
_posts.clear()
with patch("subprocess.run", side_effect=[
        Mock(returncode=1, stdout="", stderr="cat: startup.exitcode: No such file"),
        Mock(returncode=0, stdout="3\n", stderr="")]) as _run:
    tf._watch_startup_script(36, "pb-pytorch-1", "/home/jovyan/work")
ok("the exit code is read inside the container once the script finishes",
   _run.call_args.args[0] == ["docker", "exec", "pb-pytorch-1", "cat",
                              "/home/jovyan/work/startup.exitcode"])
ok("and reported once to the server (timeline + optional auto-stop)",
   _posts == [("/jobs/startup_done", {"task_id": 36, "exit_code": 3})])
ok("and logged for the buyer", any("exited with code 3" in m for m in _logs))
with tf._pb_vm_lock:
    tf._pb_vm_watch.pop(36)
_posts.clear()
with patch("subprocess.run") as _run:
    tf._watch_startup_script(36, "pb-pytorch-1", "/home/jovyan/work")
ok("nothing is probed or reported for a rental that already ended", not _run.called and not _posts)

_logs.clear()
with patch("subprocess.run", side_effect=subprocess.CalledProcessError(
        1, ["docker", "exec"], "", "sh: can't create .pb-startup.sh: Read-only file system")):
    tf._start_startup_script(37, "pb-pytorch-2", "/work", "echo hi")
ok("a script that cannot start says why and leaves the rental up",
   any(m.startswith("startup script could not start") and "Read-only" in m for m in _logs))

# ------------------------------------------------------------------ sealed rental secrets
import json

import workload_seal as _ws

_aes = _ws.new_aes_key()
_SECRET = "AGE-SECRET-KEY-1-NEVER-IN-ARGV"
_TOKEN = "prt_eyJ-rental-storage-token"
_sealed = _ws.seal(_aes, json.dumps({"AUDIO_KEY": _SECRET, "PETABYTE_STORAGE_TOKEN": _TOKEN}).encode(),
                   aad=b"40")
tf._SEAL_AES = _aes
_logs.clear()
_results.clear()
_order = []
tf._watch_startup_script = lambda tid, name, workdir: None


_env_files = []


def _fake_run(argv, **kw):
    if argv[:2] == ["docker", "run"] and "--env-file" in argv:   # read it before the agent deletes it
        with open(argv[argv.index("--env-file") + 1]) as fh:
            _env_files.append(fh.read())
    _order.append(("script" if ".pb-startup.sh" in str(argv) else
                   "secret" if "/run/secrets/" in str(argv[-1]) else "other", argv, kw))
    return Mock(returncode=0, stdout="cid40\n", stderr="")


with patch("shutil.which", return_value="/usr/bin/docker"), patch("subprocess.run", side_effect=_fake_run):
    tf._run_template(dict(_task, task_id=40, template="pytorch", cache="/home/jovyan/work",
                          secrets_sealed=_sealed, env={"MODEL_SIZE": "large"},
                          params={"startup_script": "cat /run/secrets/AUDIO_KEY >/dev/null"}))
_docker_run = next(a for k, a, _ in _order if a[:2] == ["docker", "run"])
# mode=1777 (sticky, like /tmp) is explicit and deliberate: docker exec runs as the image's own user
# (root or e.g. jovyan), which must be able to create its 0400 files there; a root-owned 0700 dir
# would lock a non-root image's startup script out of its own secrets.
ok("secrets: the container gets an in-memory tmpfs at /run/secrets with explicit options",
   "--tmpfs" in _docker_run
   and _docker_run[_docker_run.index("--tmpfs") + 1]
   == "/run/secrets:rw,noexec,nosuid,nodev,size=1m,mode=1777")
ok("secrets: no value reaches the docker argv or any -e flag",
   not any(_SECRET in str(a) or _TOKEN in str(a) for _, a, _ in _order) and "-e" not in _docker_run)
ok("secrets: the --env-file holds the plain env only, never a secret or the token",
   len(_env_files) == 1 and "MODEL_SIZE=large" in _env_files[0]
   and _SECRET not in _env_files[0] and _TOKEN not in _env_files[0])
_writes = [(a, kw) for k, a, kw in _order if k == "secret"]
ok("secrets: each one is written with docker exec -i, the value on stdin, umask 277 (0400)",
   len(_writes) == 2 and all(a[:3] == ["docker", "exec", "-i"] and "umask 277" in a[-1] for a, _ in _writes)
   and {kw.get("input") for _, kw in _writes} == {_SECRET.encode(), _TOKEN.encode()})
ok("secrets: the storage token lands as /run/secrets/PETABYTE_STORAGE_TOKEN",
   any(a[-1].endswith("/run/secrets/PETABYTE_STORAGE_TOKEN") for a, _ in _writes))
_kinds = [k for k, _, _ in _order]
ok("secrets: written BEFORE the startup script runs",
   "script" in _kinds and max(i for i, k in enumerate(_kinds) if k == "secret") < _kinds.index("script"))
ok("secrets: never in an agent log line; the log says where they went",
   not any(_SECRET in m or _TOKEN in m for m in _logs) and any("/run/secrets" in m for m in _logs))

# Fail closed: a blob sealed for another task, or a write that fails, fails the launch (refunded).
_logs.clear()
_results.clear()
with patch("shutil.which", return_value="/usr/bin/docker"), \
     patch("subprocess.run", return_value=Mock(returncode=0, stdout="cid41\n", stderr="")) as _run:
    tf._run_template(dict(_task, task_id=41, template="pytorch", secrets_sealed=_sealed, params={}))
ok("secrets: a blob bound to another task id is refused before any container starts",
   not any(c.args[0][:2] == ["docker", "run"] for c in _run.call_args_list)
   and _results and _results[-1]["status"] == "failed" and "unsealed" in _results[-1]["result"])
_logs.clear()
_results.clear()


def _write_fails(argv, **kw):
    if "/run/secrets/" in str(argv[-1]):
        raise subprocess.CalledProcessError(1, argv, "", "sh: can't create: " + _SECRET)
    return Mock(returncode=0, stdout="cid42\n", stderr="")


_sealed42 = _ws.seal(_aes, json.dumps({"AUDIO_KEY": _SECRET}).encode(), aad=b"42")
with patch("shutil.which", return_value="/usr/bin/docker"), patch("subprocess.run", side_effect=_write_fails):
    tf._run_template(dict(_task, task_id=42, template="pytorch", secrets_sealed=_sealed42, params={}))
ok("secrets: a failed write fails the launch with a value-free reason",
   _results and _results[-1]["status"] == "failed" and "/run/secrets" in _results[-1]["result"]
   and not any(_SECRET in str(x) for x in _results + _logs))
tf._SEAL_AES = None
tf.HEARTBEAT_S = 0
_results.clear()
with patch("shutil.which", return_value="/usr/bin/docker"), \
     patch("subprocess.run", return_value=Mock(returncode=0, stdout="cid43\n", stderr="")) as _run:
    tf._run_template(dict(_task, task_id=43, template="pytorch", secrets_sealed=_sealed, params={}))
ok("secrets: no seal key on the node -> launch refused, no container",
   not any(c.args[0][:2] == ["docker", "run"] for c in _run.call_args_list)
   and _results and "no seal key" in _results[-1]["result"])

print()
print("=== template launch: " + ("0 failures" if _fail == 0 else str(_fail) + " FAILED") + " ===")
raise SystemExit(1 if _fail else 0)
