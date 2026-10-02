"""image_snapshot_agent_test.py — the agent side of buyer image snapshots, offline.

  * docker commit scrubs every platform-injected env var (and token-like ones the image set),
    empties pb.* labels and resets CMD to the base image's; an unscrubbable key refuses;
  * docker save is streamed into presigned parts (one part in memory at a time) with the right
    sha256, part numbers and ETags; over the size cap it stops and kills docker save;
  * bake reports complete (sha256 + size + image id) and removes the committed image; a failure
    goes to the buyer's job log and /jobs/snapshot_failed;
  * load verifies size + sha256 BEFORE docker load and the image id AFTER; any mismatch refuses,
    and _run_template then never reaches `docker run`;
  * the heartbeat hands each snapshot id to one bake only.

Docker, the API and S3 are fakes; payloads are a few KB. Run: python image_snapshot_agent_test.py
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("PETABYTE_API_URL", "http://localhost")
os.environ.setdefault("PETABYTE_API_KEY", "test")
os.environ.setdefault("PETABYTE_SPEC_ID", "1")
os.environ["PB_DISK_RESERVE_GB"] = "0"
import httpx  # noqa: E402
import image_snapshot as snap  # noqa: E402
import template_storage  # noqa: E402

TMP = tempfile.mkdtemp(prefix="pbsnap-test-")
snap._TMP_DIR = TMP
template_storage.STATE = __import__("pathlib").Path(TMP) / "images.json"
_fail = 0


def ok(label, cond, extra=""):
    global _fail
    print(("ok   " if cond else "FAIL ") + label + (f"   [{str(extra)[:300]}]" if extra and not cond else ""))
    if not cond:
        _fail += 1


def resp(code=200, body=None, headers=None, content=b""):
    return httpx.Response(code, json=body, headers=headers, content=None if body is not None else content,
                          request=httpx.Request("POST", "http://localhost"))


# ------------------------------------------------------------------ commit argv
print("-- commit --")
BASE = {"Config": {"Env": ["PATH=/usr/bin", "GPG_KEY=abc", "HF_TOKEN=image-baked"], "Cmd": ["start-notebook.sh"]}}
INFO = {"Image": "sha256:" + "1" * 64, "Config": {
    "Env": ["PATH=/usr/bin", "GPG_KEY=abc", "HF_TOKEN=image-baked", "JUPYTER_TOKEN=s3cret",
            "PASSWORD=pw", "PETABYTE_STORAGE_TOKEN=pst_x", "OLLAMA_MODEL=qwen"],
    "Cmd": ["start-notebook.sh", "--NotebookApp.token=s3cret"],
    "Labels": {"pb.task": "7", "pb.vm_id": "abc", "maintainer": "jupyter"}}}
argv = snap.commit_argv("cid1", INFO, BASE)
changes = [argv[i + 1] for i, a in enumerate(argv) if a == "--change"]
ok("every injected env var is emptied",
   {"ENV JUPYTER_TOKEN=", "ENV PASSWORD=", "ENV PETABYTE_STORAGE_TOKEN=", "ENV OLLAMA_MODEL="} <= set(changes), changes)
ok("a token the image itself set is emptied too", "ENV HF_TOKEN=" in changes)
ok("unchanged image env (PATH, GPG_KEY) is kept", not any(x.startswith(("ENV PATH", "ENV GPG_KEY")) for x in changes))
ok("pb.* labels are emptied, others kept", 'LABEL pb.task="" pb.vm_id=""' in changes and not any("maintainer" in x for x in changes))
ok("CMD is reset to the base image's (drops this rental's run args)", 'CMD ["start-notebook.sh"]' in changes)
ok("argv is docker commit … <container>", argv[:2] == ["docker", "commit"] and argv[-1] == "cid1")
ok("no secret value ever reaches the argv", not any("s3cret" in a or "pst_x" in a for a in argv))
try:
    snap.commit_argv("c", {"Config": {"Env": ["BAD KEY=x"]}}, {"Config": {}})
    ok("an env key that can't be scrubbed safely refuses", False)
except ValueError:
    ok("an env key that can't be scrubbed safely refuses", True)

# ------------------------------------------------------------------ streaming upload
print("-- upload --")
DATA = bytes(range(256)) * 10                       # 2560 bytes -> parts of 1000, 1000, 560
_real_popen = subprocess.Popen


def fake_save(n_bytes):
    code = f"import sys; sys.stdout.buffer.write((bytes(range(256)) * 100)[:{n_bytes}])"
    return lambda argv, **kw: _real_popen([sys.executable, "-c", code], **kw)


def api(record, part_status=200):
    def post(path, payload):
        record.append((path, payload))
        if path == "/jobs/snapshot_part_url":
            return resp(part_status, {"upload_url": f"https://s3.test/part{payload['part_number']}"})
        return resp(200, {"status": "ok"})
    return post


puts = []


def put(url, content=None, **kw):
    puts.append((url, content))
    return resp(200, content=b"", headers={"ETag": f'"etag-{len(puts)}"'})


calls = []
with patch.object(snap.subprocess, "Popen", side_effect=fake_save(len(DATA))):
    size, sha, parts = snap.upload("sha256:" + "2" * 64, "a" * 16, 5, api(calls), put, part_size=1000, max_bytes=10_000)
ok("sha256 and size are of the whole docker-save stream", size == len(DATA) and sha == hashlib.sha256(DATA).hexdigest())
ok("parts are 1..n with their ETags", parts == [{"part_number": 1, "etag": '"etag-1"'},
                                                 {"part_number": 2, "etag": '"etag-2"'},
                                                 {"part_number": 3, "etag": '"etag-3"'}], parts)
ok("each part went to its own presigned URL, in order, and reassembles the stream",
   [u for u, _ in puts] == ["https://s3.test/part1", "https://s3.test/part2", "https://s3.test/part3"]
   and b"".join(c for _, c in puts) == DATA and max(len(c) for _, c in puts) == 1000)
ok("part URLs were asked for this task + snapshot",
   all(p == {"task_id": 5, "snapshot_id": "a" * 16, "part_number": i + 1} for i, (_, p) in enumerate(calls)))

procs = []


def tracked(argv, **kw):
    p = fake_save(5000)(argv, **kw)
    procs.append(p)
    return p


puts.clear()
try:
    with patch.object(snap.subprocess, "Popen", side_effect=tracked):
        snap.upload("sha256:" + "2" * 64, "a" * 16, 5, api([]), put, part_size=1000, max_bytes=2500)
    ok("over the size cap the upload stops", False)
except ValueError as e:
    ok("over the size cap the upload stops with a buyer-safe reason", "snapshot limit" in str(e), e)
ok("…after at most the parts under the cap, and docker save is not left running",
   len(puts) <= 2 and procs and procs[0].poll() is not None)

# ------------------------------------------------------------------ bake end to end
print("-- bake --")
IMG = "sha256:" + "3" * 64


def fake_docker(ps_out="cid1"):
    def _d(*args, timeout=120):
        if args[0] == "ps":
            return ps_out
        if args[0] == "inspect":
            return json.dumps([INFO])
        if args[:2] == ("image", "inspect"):
            return json.dumps([BASE])
        if args[0] == "commit":
            fake_docker.commit = args
            return IMG
        raise AssertionError(args)
    return _d


logs, calls, removed = [], [], []
puts.clear()
with patch.object(snap, "_docker", side_effect=fake_docker()), \
     patch.object(snap.subprocess, "Popen", side_effect=fake_save(len(DATA))), \
     patch.object(snap.subprocess, "run", side_effect=lambda argv, **kw: removed.append(argv)):
    snap.bake({"id": "b" * 16, "task_id": 9, "part_size": 1000, "max_bytes": 10_000}, api(calls),
              lambda tid, m: logs.append(m), put)
done = [p for path, p in calls if path == "/jobs/snapshot_complete"]
ok("bake reports complete with sha256, size, image id and the parts",
   done and done[0]["sha256"] == hashlib.sha256(DATA).hexdigest() and done[0]["size_bytes"] == len(DATA)
   and done[0]["image_id"] == IMG and len(done[0]["parts"]) == 3, calls[-1:])
ok("…committing with the scrub changes", "ENV JUPYTER_TOKEN=" in fake_docker.commit)
ok("…then removes the committed image locally", ["docker", "image", "rm", IMG] in removed)
ok("…and tells the buyer", any("saved" in m for m in logs), logs)

logs, calls = [], []
with patch.object(snap, "_docker", side_effect=fake_docker(ps_out="")), \
     patch.object(snap.subprocess, "run", side_effect=lambda argv, **kw: None):
    snap.bake({"id": "c" * 16, "task_id": 9}, api(calls), lambda tid, m: logs.append(m), put)
fail = [p for path, p in calls if path == "/jobs/snapshot_failed"]
ok("no running container -> snapshot_failed with the reason",
   fail and fail[0]["snapshot_id"] == "c" * 16 and "not running" in fail[0]["reason"], calls)
ok("…and the buyer's job log says it failed", any("FAILED" in m and "not running" in m for m in logs), logs)

logs, calls = [], []
with patch.object(snap, "_docker", side_effect=fake_docker()), \
     patch.object(snap.subprocess, "Popen", side_effect=fake_save(len(DATA))), \
     patch.object(snap.subprocess, "run", side_effect=lambda argv, **kw: None):
    snap.bake({"id": "d" * 16, "task_id": 9, "part_size": 1000}, api(calls, part_status=404),
              lambda tid, m: logs.append(m), put)
ok("a deleted/cancelled snapshot (part URL 404) stops the upload and reports it",
   [p for p, _ in calls].count("/jobs/snapshot_part_url") == 1
   and any(p == "/jobs/snapshot_failed" for p, _ in calls), calls)

# ------------------------------------------------------------------ load
print("-- load --")
TAR = b"fake docker-save tarball" * 40
LID = "sha256:" + "4" * 64
GOOD = {"id": "e" * 16, "url": "https://s3.test/get", "sha256": hashlib.sha256(TAR).hexdigest(),
        "image_id": LID, "size": len(TAR)}


class FakeStream:
    def __init__(self, body):
        self.body = body

    def __call__(self, method, url, **kw):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        pass

    def iter_bytes(self, n):
        for i in range(0, len(self.body), 100):
            yield self.body[i:i + 100]


def load_docker(loaded_id=LID):
    seen = []

    def _d(*args, timeout=120):
        seen.append(args)
        if args[:2] == ("image", "inspect"):
            raise subprocess.CalledProcessError(1, args)       # not cached yet
        if args[0] == "load":
            ok("docker load reads a fully written, verified file", open(args[2], "rb").read() == TAR)
            return f"Loaded image ID: {loaded_id}"
        if args[0] == "tag":
            return ""
        raise AssertionError(args)
    return _d, seen


d, seen = load_docker()
with patch.object(snap, "_docker", side_effect=d):
    tag = snap.load(GOOD, 11, stream=FakeStream(TAR))
ok("a verified snapshot loads and is tagged pbsnap/<id>", tag == f"pbsnap/{'e' * 16}"
   and ("tag", LID, tag) in seen, seen)
ok("the downloaded tar is removed afterwards", os.listdir(TMP) in ([], ["images.json"], ["images.lock", "images.json"])
   or not any(f.endswith(".tar") for f in os.listdir(TMP)), os.listdir(TMP))

for label, body, rec in (("a sha256 mismatch", b"X" + TAR[1:], GOOD),
                         ("a download longer than recorded", TAR + b"more", GOOD)):
    d, seen = load_docker()
    try:
        with patch.object(snap, "_docker", side_effect=d):
            snap.load(rec, 11, stream=FakeStream(body))
        ok(f"{label} refuses", False)
    except ValueError:
        ok(f"{label} refuses BEFORE docker load", not any(a[0] == "load" for a in seen), seen)

d, seen = load_docker(loaded_id="sha256:" + "5" * 64)
removed = []
try:
    with patch.object(snap, "_docker", side_effect=d), \
         patch.object(snap.subprocess, "run", side_effect=lambda argv, **kw: removed.append(argv)):
        snap.load(GOOD, 11, stream=FakeStream(TAR))
    ok("an image id mismatch refuses", False)
except ValueError as e:
    ok("an image id mismatch refuses, never tags, and removes what it loaded",
       "does not match" in str(e) and not any(a[0] == "tag" for a in seen)
       and ["docker", "image", "rm", "sha256:" + "5" * 64] in removed, (seen, removed))

# ------------------------------------------------------------------ task_fetcher wiring
print("-- task_fetcher --")
import task_fetcher as tf  # noqa: E402
_logs, _results = [], []
tf.report_log = lambda tid, msg: _logs.append(msg)
tf._post = lambda path, payload: None
tf._post_result_ack_retry = lambda payload, attempts=4: (_results.append(payload), True)[1]
tf._signed_result = lambda tid, status="completed", result=None, **k: {"task_id": tid, "status": status, "result": result}
tf._cleanup_job_resources = lambda tid, name=None: None
tf.template_storage.prepare = lambda *a, **k: (_ for _ in ()).throw(AssertionError("no registry pull for a snapshot"))
tf._start_storage_guard = lambda *a: None
tf._isolation_flags = lambda task: []
tf._reverse_tunnel_enabled = lambda: False
tf._pb_vm_started["on"] = True
runs = []
d, seen = load_docker()
with patch("shutil.which", return_value="/usr/bin/docker"), \
     patch.object(snap, "_docker", side_effect=d), \
     patch.object(snap.httpx, "stream", FakeStream(b"X" + TAR[1:])), \
     patch("subprocess.run", side_effect=lambda argv, **kw: runs.append(argv) or Mock(returncode=0, stdout="cid\n", stderr="")):
    tf._run_template({"task_id": 41, "template": "jupyter", "image": "pbsnap/" + "e" * 16, "egress": "none",
                      "params": {}, "snapshot": dict(GOOD, id="9" * 16)})
ok("a tampered snapshot never reaches docker run", not any(a[:2] == ["docker", "run"] for a in runs), runs)
ok("…and the rental fails with the reason in the buyer's log",
   _results and _results[-1]["status"] == "failed" and any("integrity check failed" in m for m in _logs), _logs)

runs.clear()
d, seen = load_docker()
with patch("shutil.which", return_value="/usr/bin/docker"), \
     patch.object(snap, "_docker", side_effect=d), \
     patch.object(snap.httpx, "stream", FakeStream(TAR)), \
     patch("subprocess.run", side_effect=lambda argv, **kw: runs.append(argv) or Mock(returncode=0, stdout="cid\n", stderr="")):
    tf._run_template({"task_id": 42, "template": "jupyter", "image": "pbsnap/" + "8" * 16, "egress": "none",
                      "params": {}, "snapshot": dict(GOOD, id="8" * 16)})
run = next((a for a in runs if a[:2] == ["docker", "run"]), [])
ok("a verified snapshot runs as pbsnap/<id> with --pull=never", "--pull=never" in run and run[-1] == "pbsnap/" + "8" * 16, run)

started = []
with patch.object(snap, "bake", side_effect=lambda e, post, log: started.append(e["id"])):
    tf._handle_snapshot({"id": "7" * 16, "task_id": 3})
    tf._handle_snapshot({"id": "7" * 16, "task_id": 3})       # re-sent by the next heartbeat
    for t in threading.enumerate():
        if t.name.startswith("pb-snapshot-"):
            t.join(5)
ok("the heartbeat starts one bake per snapshot id", started == ["7" * 16], started)

print(("PASS" if _fail == 0 else f"FAIL ({_fail})"))
raise SystemExit(1 if _fail else 0)
