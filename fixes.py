"""Signed support fixes on the seller node (phase 2 of diagnostics; server: lumaris_api/node_fixes.py).

The agent only CARRIES a fix: it drops the payload + signature a heartbeat delivered into
STATE/fix-inbox, and petabyte-agent-fix.path starts fix-runner.sh as root. The RUNNER is the
security boundary: it runs a fix only if the owner opted in, the Ed25519 signature verifies against
the pinned release key, the payload names this node, hasn't expired and never ran here. The agent
then reports what the runner did (masked) and re-checks the GPU.

Owner consent: `main.py fixes enable` (asked once at install), `fixes disable`, `fixes status`.
"""
import base64
import os
import re
import shutil
import subprocess
import sys

STATE = "/var/lib/petabyte-agent"            # the agent unit's StateDirectory; fix-runner.sh uses it too
ENV_FILE = "/etc/petabyte/agent.env"
APP = "/opt/petabyte-agent"
UNIT_DIR = "/etc/systemd/system"
UNITS = ("petabyte-agent-fix.path", "petabyte-agent-fix.service")
_OUTPUT_MAX = 50_000


def _p(name):
    return os.path.join(STATE, name)


def _env_value(key):
    try:
        with open(ENV_FILE) as f:
            vals = [l.split("=", 1)[1].strip().strip("\"'") for l in f if l.startswith(key + "=")]
        return vals[-1] if vals else ""
    except OSError:
        return ""


def enabled():
    """Owner opted in AND the runner is installed (`fixes enable` does both). Read live each
    heartbeat, so enabling/disabling needs no agent restart."""
    return _env_value("PB_ALLOW_REMOTE_FIXES") == "true" and os.path.exists(os.path.join(UNIT_DIR, UNITS[0]))


def _seen(fid):
    if os.path.exists(_p(f"fix-inbox/{fid}.payload")) or os.path.exists(_p(f"fix-outbox/{fid}.rc")):
        return True
    try:
        with open(_p("fixes-done")) as f:
            return str(fid) in f.read().split()
    except OSError:
        return False


def pending():
    """A fix is queued, running, or its result isn't reported yet: the node takes no new work."""
    for d, suffix in (("fix-inbox", ".payload"), ("fix-outbox", ".rc")):
        try:
            if any(n.endswith(suffix) for n in os.listdir(_p(d))):
                return True
        except OSError:
            pass
    return False


def handle(fix, rental_live=False):
    """Queue a delivered fix for the runner. Never while a job or rental may be on the machine
    (`rental_live`; a fix may restart Docker); the server keeps offering it until it expires, and
    once queued the node stops taking jobs until its result is reported (pending()). True if queued."""
    if not fix or rental_live or not enabled():
        return False
    try:
        fid = int(fix["id"])
        payload = base64.b64decode(fix["payload_b64"], validate=True)
        sig = base64.b64decode(fix["signature"], validate=True)
    except Exception:                                        # noqa: BLE001 — malformed: ignore
        return False
    if fid <= 0 or _seen(fid):
        return False
    inbox = _p("fix-inbox")
    os.makedirs(inbox, mode=0o700, exist_ok=True)
    for name, data in ((f"{fid}.sig", sig), (f"{fid}.payload", payload)):   # payload last: the trigger
        tmp = os.path.join(inbox, f".{name}.tmp")
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, os.path.join(inbox, name))
    return True


def results():
    """[(fix_id, rc, output)] the runner finished; rc is an exit code or 'rejected: <why>'."""
    out = []
    try:
        names = sorted(os.listdir(_p("fix-outbox")))
    except OSError:
        return out
    for name in names:
        m = re.fullmatch(r"(\d+)\.rc", name)
        if not m:
            continue
        fid = int(m.group(1))
        try:
            with open(_p(f"fix-outbox/{name}")) as f:
                rc = f.read().strip()[:200]
        except OSError:
            continue
        try:
            with open(_p(f"fix-outbox/{fid}.out"), errors="replace") as f:
                output = f.read()[-_OUTPUT_MAX:]
        except OSError:
            output = ""
        out.append((fid, rc, output))
    return out


def ack(fid):
    for ext in ("rc", "out"):
        try:
            os.remove(_p(f"fix-outbox/{fid}.{ext}"))
        except OSError:
            pass


def _set_env(key, value):
    try:
        with open(ENV_FILE) as f:
            lines = [l for l in f.read().splitlines() if not l.startswith(key + "=")]
    except OSError:
        lines = []
    lines.append(f"{key}={value}")
    tmp = ENV_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(lines) + "\n")
    if os.path.exists(ENV_FILE):
        shutil.copymode(ENV_FILE, tmp)
    os.replace(tmp, ENV_FILE)


_CONSENT = ("Petabyte support may apply fixes to this machine when its GPU setup breaks. Every fix is a\n"
            "script signed for THIS machine only (our release key, the same one that signs agent\n"
            "updates), expires within 24 hours, runs once, never while a job or rental is running (the\n"
            "machine takes no new jobs until it finishes), and its output is reported back to\n"
            "support. We email you before a fix is applied.\n"
            "Turn it off any time: sudo /opt/petabyte-agent/.venv/bin/python /opt/petabyte-agent/main.py fixes disable")


def control(action, assume_yes=False):
    """`main.py fixes enable|disable|status`."""
    if action == "status":
        print("support fixes:", "ON" if enabled() else "OFF")
        return 0
    if os.geteuid() != 0:
        raise SystemExit("Run with sudo.")
    if action == "enable":
        print(_CONSENT)
        if not assume_yes and input("Allow support fixes on this machine? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Not enabled.")
            return 1
        _set_env("PB_ALLOW_REMOTE_FIXES", "true")
        for u in UNITS:
            shutil.copyfile(os.path.join(APP, u), os.path.join(UNIT_DIR, u))
        subprocess.run(["systemctl", "daemon-reload"], check=False)
        subprocess.run(["systemctl", "enable", "--now", UNITS[0]], check=False)
        print("Support fixes ON.")
        return 0
    if action == "disable":
        _set_env("PB_ALLOW_REMOTE_FIXES", "false")
        subprocess.run(["systemctl", "disable", "--now", UNITS[0]], check=False)
        for u in UNITS:
            try:
                os.remove(os.path.join(UNIT_DIR, u))
            except OSError:
                pass
        subprocess.run(["systemctl", "daemon-reload"], check=False)
        print("Support fixes OFF.")
        return 0
    print("Usage: main.py fixes [status|enable|disable] [--yes]", file=sys.stderr)
    return 2
