"""agent_self_update_test.py -- an outdated agent updates itself when the server says a newer
SIGNED bundle is served, between jobs, and never in a way that bypasses the signature or the
seller's auto-update opt-out.

Prod 2026-09-26: node 45 ran the pre-#579 render code (the desktop-image hang) for hours after the
fix was deployed, because the only update trigger was the 6-hourly petabyte-agent-update.timer.
The heartbeat now carries `agent_update: {required, bundle}` and job_loop starts the SAME signed
update unit before claiming the next job.

  1. the heartbeat body is read defensively (absent / malformed -> nothing wanted);
  2. an update is started only when wanted, not already applied, and the timer is enabled
     (PETABYTE_AUTO_UPDATE=false leaves it uninstalled: opted-out sellers are never forced);
  3. while update.sh runs, claims are held so its agent restart never lands mid-job;
  4. a start that did not land is retried at most every _UPDATE_RETRY_S, and the node keeps serving;
  5. the agent only ever starts the unit: update.sh keeps the pinned-key signature check.

Offline: systemctl and the bundle file are stubbed. Run: python agent_self_update_test.py
"""
import os
import sys
import types
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("PETABYTE_API_URL", "http://localhost")
os.environ.setdefault("PETABYTE_API_KEY", "test")
os.environ.setdefault("PETABYTE_SPEC_ID", "1")
# The API-only SQLite CI job does not install the optional notebook runner.
# This test exercises real update/claim logic, never notebook execution. Fail
# loudly if a future change unexpectedly tries to execute a notebook here.
_notebook = types.ModuleType("notebook")


def _unused_notebook(*args, **kwargs):
    raise AssertionError("self-update tests must not execute notebooks")


_notebook.run_notebook_code = _unused_notebook
sys.modules["notebook"] = _notebook
import task_fetcher as tf

_fail = 0


def ok(label, cond, extra=""):
    global _fail
    print(("ok   " if cond else "FAIL ") + label + (f"   [{extra}]" if extra and not cond else ""))
    if not cond:
        _fail += 1


OLD, NEW = "a" * 64, "b" * 64
calls = []
state = {"active": "inactive", "enabled": "enabled", "start_rc": 0, "bundle": OLD}


def fake_systemctl(*args):
    calls.append(args)
    if args[0] == "is-active":
        return types.SimpleNamespace(returncode=0, stdout=state["active"] + "\n", stderr="")
    if args[0] == "is-enabled":
        return types.SimpleNamespace(returncode=0, stdout=state["enabled"] + "\n", stderr="")
    if args[0] == "start":
        return types.SimpleNamespace(returncode=state["start_rc"], stdout="", stderr="boom")
    raise AssertionError(args)


tf._systemctl = fake_systemctl
tf._agent_bundle = lambda: state["bundle"]


def reset(want=None):
    calls.clear()
    tf._AGENT_UPDATE.update({"wanted": want, "started": 0.0})


def started():
    return [c for c in calls if c[0] == "start"]


# 1) the heartbeat body
reset()
tf._note_agent_update({"agent_update": {"required": True, "bundle": NEW}})
ok("a required update with a 64-hex bundle is remembered", tf._AGENT_UPDATE["wanted"] == NEW)
for body, why in (({}, "absent (old API)"), ({"agent_update": {"required": False, "bundle": NEW}}, "not required"),
                  ({"agent_update": {"required": True, "bundle": "short"}}, "malformed bundle"),
                  ({"agent_update": "yes"}, "not a dict"), (None, "no body")):
    tf._note_agent_update(body)
    ok(f"...and cleared when {why}", tf._AGENT_UPDATE["wanted"] is None)

# 2) nothing wanted / already applied -> no systemctl at all, claims flow
reset(None)
ok("no update wanted: claims are not held", tf._self_update_holds_claims() is False and not calls)
reset(OLD)
ok("the wanted bundle is the one already applied: nothing runs", tf._self_update_holds_claims() is False and not calls)

# the normal path
reset(NEW)
ok("an outdated agent holds claims and starts the update", tf._self_update_holds_claims() is True)
ok("...by starting ONLY the signed update unit, non-blocking",
   started() == [("start", "--no-block", "petabyte-agent-update.service")], str(started()))

# 3) while update.sh runs, keep holding claims (its restart must not land mid-job)
state["active"] = "activating"
calls.clear()
ok("while update.sh is running, claims stay held", tf._self_update_holds_claims() is True)
ok("...without starting it a second time", not started())
state["active"] = "inactive"

# 4) it ran but the node is still old (e.g. a refused signature): serve jobs, retry later
calls.clear()
ok("a finished update that did not land releases claims (the node keeps serving)",
   tf._self_update_holds_claims() is False and not started())
tf._AGENT_UPDATE["started"] -= tf._UPDATE_RETRY_S + 1
ok("...and it is retried once the retry window has passed", tf._self_update_holds_claims() is True and started())

# opt-out: PETABYTE_AUTO_UPDATE=false leaves the timer uninstalled -> never forced
for en in ("disabled", "not-found", ""):
    reset(NEW)
    state["enabled"] = en
    ok(f"auto-update timer {en or 'missing'!r}: the seller opted out, nothing is started",
       tf._self_update_holds_claims() is False and not started())
state["enabled"] = "enabled"

# no systemd at all (Windows / macOS / container): nothing runs, nothing crashes
reset(NEW)
tf._systemctl = lambda *a: None
ok("no systemctl on this host: claims flow, nothing crashes", tf._self_update_holds_claims() is False)
tf._systemctl = fake_systemctl

# a start that fails to launch does not hold claims
reset(NEW)
state["start_rc"] = 1
ok("a unit that fails to start does not block job claims", tf._self_update_holds_claims() is False)
state["start_rc"] = 0

# 5) the signature check stays in update.sh (the agent only ever starts the unit)
_sh = (Path(tf.__file__).resolve().parent / "update.sh").read_text()
ok("update.sh still refuses a bundle whose signature does not verify (no unsigned path)",
   "verify_bundle" in _sh and "refusing update" in _sh and "no unsigned fallback" in _sh)
_src = Path(tf.__file__).read_text()
ok("job_loop checks for a self-update before claiming a job",
   _src.index("_self_update_holds_claims()") < _src.index('f"{API_URL}/jobs/next"', _src.index("def job_loop")))
ok("the heartbeat hands its body to _note_agent_update", "_note_agent_update(_body)" in _src)

print()
print("=== agent_self_update: " + ("0 failures" if _fail == 0 else f"{_fail} FAILED") + " ===")
raise SystemExit(1 if _fail else 0)
