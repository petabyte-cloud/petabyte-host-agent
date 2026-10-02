"""A seller reboot mid-rental restarts the buyer's container instead of failing the rental.

Prod booking 320 (2026-10-02): the RTX 2060 host was switched off and on. After boot the agent's
watchdog found the jupyter container 'exited (255)' (Docker's code for a container whose daemon
died under it), reported container_exited_exit_255 as a job failure and deleted the container and
its workspace volume, with 25 paid hours left. Run: python host_reboot_restore_test.py
"""
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
import network_policy  # noqa: E402

from datetime import datetime, timezone  # noqa: E402

BOOT = int(datetime(2026, 10, 2, 7, 56, tzinfo=timezone.utc).timestamp())   # the reboot
calls, posted, cleaned, ensured = [], [], [], []
state = {}                             # container -> (status, exit code, FinishedAt)


def _run(cmd, **kw):
    calls.append(cmd)
    if cmd[:2] == ["docker", "ps"]:
        return types.SimpleNamespace(returncode=0, stdout="\n".join(state) + "\n", stderr="")
    if cmd[:2] == ["docker", "start"]:
        status, code, fin = state[cmd[2]]
        state[cmd[2]] = ("running", "0", fin)
        return types.SimpleNamespace(returncode=0, stdout=cmd[2], stderr="")
    if cmd[:2] == ["docker", "inspect"] and cmd[-1] in state:
        status, code, fin = state[cmd[-1]]
        fmt = cmd[3]
        if fmt.startswith("{{.State.Status}}|"):   # host-stop check
            return types.SimpleNamespace(returncode=0, stdout=f"{status}|{code}|{fin}|pb-net-t{cmd[-1][1:]}\n", stderr="")
        if fmt.startswith("{{.State.Status}}:"):   # watchdog
            return types.SimpleNamespace(returncode=0, stdout=f"{status}:{code}\n", stderr="")
        return types.SimpleNamespace(returncode=0, stdout="||||||\n", stderr="")   # labels: none
    if cmd[:2] == ["docker", "inspect"]:
        return types.SimpleNamespace(returncode=1, stdout="", stderr="Error: No such object: " + cmd[-1])
    return types.SimpleNamespace(returncode=0, stdout="", stderr="")


tf.subprocess.run = _run
tf._boot_time = lambda: BOOT
tf._pb_vm_started["on"] = True          # no background watchdog thread: the test sweeps by hand
tf.report_log = lambda tid, line: None
tf._post_result_ack = lambda payload: (posted.append(payload), True)[1]
tf._signed_result = lambda tid, status="completed", result=None, **k: {
    "task_id": tid, "status": status, "result": result, **k}
tf._cleanup_job_resources = lambda tid, name=None: cleaned.append((tid, name))
tf._container_label_task = lambda cid: cid[1:]
network_policy.ensure = lambda tid, **k: ensured.append(tid) or f"pb-net-t{tid}"
_er.knows = lambda tid: True
_er.forget = lambda tid: None

# c680: the booking-320 container — daemon died under it (exit 255).
# c681: a SIGTERM'd container that stopped before this boot (systemd stopped docker on shutdown).
# c682: an app that crashed AFTER boot while the agent was down — a real failure.
state.update({"c680": ("exited", "255", "2026-10-02T07:54:50.123456789Z"),
              "c681": ("exited", "143", "2026-10-02T07:55:10.5Z"),
              "c682": ("exited", "1", "2026-10-02T07:59:00Z")})
tf._restore_vm_watch()
started = [c[2] for c in calls if c[:2] == ["docker", "start"]]
ok("the exit-255 rental container is started again after the reboot", "c680" in started)
ok("so is one Docker stopped before this boot (SIGTERM on shutdown)", "c681" in started)
ok("an app that crashed after boot is NOT restarted", "c682" not in started)
ok("the job bridge's firewall is re-applied before a restart", ensured[:2] == [680, 681])
tf._pb_vm_scan()
ok("the watchdog reports nothing for the restarted rentals (no failure, no teardown)",
   not any(p["task_id"] in (680, 681) for p in posted) and 680 in tf._pb_vm_watch
   and 681 in tf._pb_vm_watch and not any(t in (680, 681) for t, _ in cleaned))
ok("the real crash is still reported failed",
   any(p["task_id"] == 682 and p["status"] == "failed" for p in posted))

# The firewall can't be restored -> fail closed: leave it stopped, the watchdog reports it.
posted.clear(); calls.clear(); tf._pb_vm_watch.clear()
state.clear(); state["c690"] = ("exited", "255", "2026-10-02T07:54:50Z")


def _no_net(tid, **k):
    raise RuntimeError("iptables missing")


network_policy.ensure = _no_net
tf._restore_vm_watch()
ok("no restart without the job firewall", not any(c[:2] == ["docker", "start"] for c in calls))
tf._pb_vm_scan()
ok("...and that rental is reported failed as before",
   any(p["task_id"] == 690 and p["status"] == "failed" for p in posted))

# Docker daemon down (host shutting down) is not 'container gone'.
posted.clear(); cleaned.clear(); tf._pb_vm_watch.clear()
tf._pb_vm_watch[700] = {"name": "c700", "reported": False, "miner_checked": tf.time.time()}
tf.subprocess.run = lambda cmd, **kw: types.SimpleNamespace(
    returncode=1, stdout="", stderr="Cannot connect to the Docker daemon at unix:///var/run/docker.sock")
tf._pb_vm_scan()
ok("an unreachable Docker daemon is not reported as the container being gone",
   not posted and not cleaned and 700 in tf._pb_vm_watch)
tf.subprocess.run = lambda cmd, **kw: types.SimpleNamespace(
    returncode=1, stdout="", stderr="Error: No such object: c700")
tf._pb_vm_scan()
ok("a container that really is gone is still reported",
   len(posted) == 1 and posted[0]["result"] == "container_gone_exit_1")

print(f"\n=== host_reboot_restore: {len(FAILS)} failures ===")
sys.exit(1 if FAILS else 0)
