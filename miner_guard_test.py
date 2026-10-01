"""A rental running a crypto miner is killed and reported as crypto_mining (2026-10-01 incident)."""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
FAILS = []


def ok(label, cond):
    print(("ok   " if cond else "FAIL ") + label)
    if not cond:
        FAILS.append(label)


os.environ.setdefault("PETABYTE_API_URL", "http://localhost")
os.environ.setdefault("PETABYTE_API_KEY", "test")
os.environ.setdefault("PETABYTE_SPEC_ID", "1")
import task_fetcher as tf  # noqa: E402
import execution_receipt as _er  # noqa: E402

PS = "ARGS\n/usr/bin/tini -- start-notebook.py\n/opt/conda/bin/python /opt/conda/bin/jupyter-lab\n"
ok("a normal Jupyter rental is not a miner", tf._miner_hit(PS) is None)
ok("WildRig (the 2026-10-01 case) is caught",
   tf._miner_hit(PS + "/tmp/wildrig_unpack/wildrig-multi --algo kawpow --url 166.117.41.217:9000\n"))
ok("a renamed binary pointing at a stratum pool is caught",
   tf._miner_hit(PS + "./job -o stratum+tcp://pool.example:3333 -u wallet\n"))
ok("t-rex / lolminer / xmrig are caught",
   all(tf._miner_hit(PS + f"/root/{b} --a\n") for b in ("t-rex", "lolminer", "xmrig")))
ok("a word that merely contains a miner name is not",
   tf._miner_hit(PS + "python train_rigel.py --excavator-data /data\n") is None)

# One watchdog sweep: the live rental is running a miner -> killed, reported, torn down.
calls, posted, cleaned, logs = [], [], [], []


def _run(cmd, **kw):
    calls.append(cmd)
    if cmd[:2] == ["docker", "top"]:
        return types.SimpleNamespace(returncode=0, stdout=PS + "/tmp/wildrig_unpack/wildrig-multi --algo x\n")
    return types.SimpleNamespace(returncode=0, stdout="")


tf.subprocess.run = _run
tf.report_log = lambda tid, line: logs.append(line)
tf._post_result_ack = lambda payload: (posted.append(payload), True)[1]
tf._signed_result = lambda tid, status="completed", result=None, **k: {
    "task_id": tid, "status": status, "result": result, **k}
tf._cleanup_job_resources = lambda tid, name=None: cleaned.append((tid, name))
_er.forget = lambda tid: None
tf._pb_vm_watch[7] = {"name": "c7", "reported": False}
tf._pb_vm_scan()
ok("the miner's container is killed", ["docker", "kill", "c7"] in calls)
ok("reported failed with failure_cause=crypto_mining",
   len(posted) == 1 and posted[0]["status"] == "failed" and posted[0]["failure_cause"] == "crypto_mining")
ok("the evidence (process line) is in the job log", any("wildrig-multi" in l for l in logs))
ok("and the rental is torn down", cleaned == [(7, "c7")] and 7 not in tf._pb_vm_watch)

calls.clear()
tf._pb_vm_watch[8] = {"name": "c8", "reported": False, "miner_checked": tf.time.time()}
tf.subprocess.run = lambda cmd, **kw: (calls.append(cmd), types.SimpleNamespace(
    returncode=0, stdout="running:0" if cmd[:2] == ["docker", "inspect"] else PS))[1]
tf._pb_vm_scan()
ok("checked at most once a minute per rental (no docker top inside the window)",
   not any(c[:2] == ["docker", "top"] for c in calls) and 8 in tf._pb_vm_watch)

print(f"\n=== miner_guard: {len(FAILS)} failures ===")
sys.exit(1 if FAILS else 0)
