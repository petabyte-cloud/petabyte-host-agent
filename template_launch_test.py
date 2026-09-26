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

print()
print("=== template launch: " + ("0 failures" if _fail == 0 else str(_fail) + " FAILED") + " ===")
raise SystemExit(1 if _fail else 0)
