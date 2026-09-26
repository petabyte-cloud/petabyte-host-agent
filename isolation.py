"""isolation.py — make this host able to isolate a buyer's networked app, or say exactly why not.

ONE implementation for every place that needs it, so they can never disagree:
  * install.sh   `python3 isolation.py repair` at its Docker step, and `isolation.py selftest`
                 before the node registers, so the seller sees PASS/FAIL before listing;
  * update.sh    `isolation.py repair --auto <bundle-sha>` after signed updates, unattended, as root,
                 at most once per agent version;
  * the agent    network_policy.probe() (the same _local_daemon() rule) on every heartbeat.

The rule is network_policy._local_daemon(): root on Linux, the LOCAL native Docker Engine on
/var/run/docker.sock, iptables/ip6tables/ip present. repair() applies only a change known to be safe
for the case it detected, and leaves everything else alone: the node keeps running batch jobs,
placement skips it for apps, and the reason is on its heartbeat. It never touches Docker Desktop
(on Windows the installer moves the agent into its own WSL distro instead).

Stdlib only: install.sh runs this with the system python3 before the agent's venv exists.
"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time

import network_policy as npol

STATE = "/var/lib/petabyte-agent/isolation.json"
AGENT_ENV = "/etc/petabyte/agent.env"
DROPIN = "/etc/systemd/system/petabyte-agent.service.d/10-local-docker.conf"
SELFTEST_TID = 999999999            # far above real task ids; its network/chains are removed after
_ENGINE_ON = ["systemctl", "enable", "--now", "docker"]
_ACTIONS = {
    "install": [["sh", "-c", "curl -fsSL https://get.docker.com | sh"], _ENGINE_ON],
    "enable": [_ENGINE_ON],
    "tools": [["apt-get", "install", "-y", "iptables", "iproute2"]],
}


def _is_wsl():
    try:
        with open("/proc/version") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


def _env_redirect():
    """agent.env points the agent's docker somewhere other than the local socket (and the drop-in
    that unsets it isn't installed yet)."""
    try:
        with open(AGENT_ENV) as f:
            text = f.read()
    except OSError:
        return False
    return (not os.path.exists(DROPIN)
            and bool(re.search(r"^\s*(export\s+)?DOCKER_(HOST|CONTEXT)=\S", text, re.MULTILINE)))


def inspect():
    """Read-only facts that decide the repair. Changes nothing."""
    cli = shutil.which("docker")
    facts = {"linux": sys.platform == "linux", "root": getattr(os, "geteuid", lambda: -1)() == 0,
             "wsl": _is_wsl(), "dockerd": bool(shutil.which("dockerd")),
             "systemd": os.path.isdir("/run/systemd/system"),
             "missing": [t for t in ("iptables", "ip6tables", "ip") if not shutil.which(t)],
             "redirect": _env_redirect(), "daemon": None,
             # Docker Desktop's WSL integration: its CLI and socket are symlinks into /mnt/wsl.
             "desktop": any("docker-desktop" in os.path.realpath(p)
                            for p in ("/var/run/docker.sock", cli or "") if p)}
    if cli:
        try:
            r = subprocess.run(["docker", "info", "--format", "{{json .}}"],
                               capture_output=True, text=True, timeout=15)
            if r.returncode == 0:
                info = json.loads(r.stdout)
                facts["daemon"] = {"name": info.get("Name"), "os": info.get("OSType")}
                facts["desktop"] = facts["desktop"] or info.get("Name") == "docker-desktop" \
                    or "Docker Desktop" in str(info.get("OperatingSystem"))
        except Exception:                                # noqa: BLE001 — no daemon answering
            pass
    # WSL2 distros share one network namespace: a docker0 with no daemon of ours is another
    # distro's Engine, and a second daemon would fight it over docker0 and the DOCKER chains.
    facts["bridge_taken"] = (facts["wsl"] and not facts["daemon"]
                             and os.path.exists("/sys/class/net/docker0"))
    return facts


def _native(facts):
    d = facts["daemon"]
    return bool(d) and d["name"] == socket.gethostname() and d["os"] == "linux" and not facts["desktop"]


def plan(facts):
    """([safe actions], None), or ([], why this host is left alone)."""
    if not facts["linux"]:
        return [], "a local Linux firewall is required"
    if not facts["root"]:
        return [], "the isolation repair must run as root"
    if facts["desktop"]:
        return [], npol.DESKTOP
    if facts["daemon"] and not _native(facts):
        return [], "container daemon and firewall must share the host"
    actions = []
    if not facts["daemon"]:
        if facts["bridge_taken"]:
            return [], "another Docker Engine already runs in this WSL network"
        if not facts["systemd"]:
            return [], "systemd is not running, so the Docker Engine can't be enabled"
        actions.append("enable" if facts["dockerd"] else "install")
    if facts["missing"]:
        actions.append("tools")
    if facts["redirect"]:
        actions.append("dropin")        # written only once the local Engine is verified native
    return actions, None


def selftest():
    """Build and remove a real per-job network, exactly as a rental would. (ok, reason)."""
    ok, reason = npol.probe()
    if not ok:
        return ok, reason
    try:
        npol.ensure(SELFTEST_TID)
        return True, None
    except Exception as e:                               # noqa: BLE001
        return False, str(e) or type(e).__name__
    finally:
        subprocess.run(["docker", "network", "rm", f"pb-net-t{SELFTEST_TID}"],
                       capture_output=True, timeout=30)
        npol.cleanup(SELFTEST_TID)


def last_repair():
    try:
        with open(STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _apply(action):
    if action == "dropin":
        if not _native(inspect()):
            return False                # never point the agent at a socket nothing native serves
        os.makedirs(os.path.dirname(DROPIN), exist_ok=True)
        with open(DROPIN, "w") as f:
            f.write("# Petabyte: the agent uses the local Docker Engine (isolation.py)\n"
                    "[Service]\nUnsetEnvironment=DOCKER_HOST DOCKER_CONTEXT\n")
        return subprocess.run(["systemctl", "daemon-reload"], timeout=60).returncode == 0
    return all(subprocess.run(cmd, timeout=1800).returncode == 0 for cmd in _ACTIONS[action])


def repair(version=None):
    """Detect, apply only the safe repairs, self-test, record {ok, reason, repaired}. With a version
    (the auto-update), runs at most once per agent version."""
    if version and last_repair().get("version") == version:
        print("isolation: repair already attempted for this agent version; skipping")
        return bool(last_repair().get("ok"))
    actions, reason = plan(inspect())
    if version:
        _save({"version": version, "at": int(time.time()), "ok": False, "reason": "interrupted"})
    done = []
    for action in actions:
        print(f"isolation: repair step '{action}'", flush=True)
        if not _apply(action):
            reason = f"isolation repair step '{action}' failed"
            break
        done.append(action)
    ok, why = selftest()
    reason = None if ok else (reason or why)
    if done:
        subprocess.run(["systemctl", "try-restart", "petabyte-agent"], capture_output=True, timeout=120)
    state = {"version": version, "at": int(time.time()), "actions": done, "repaired": bool(done),
             "ok": ok, "reason": reason}
    _save(state)
    print("isolation: " + json.dumps(state))
    report(ok, reason)
    return ok


def _save(state):
    try:
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        with open(STATE, "w") as f:
            json.dump(state, f)
    except OSError as e:
        print(f"isolation: could not record the result ({e})")


def report(ok, reason):
    if ok:
        print("isolation self-test: PASS — buyers' apps run here in their own isolated network")
        return
    print(f"isolation self-test: FAIL — {reason}\n"
          f"  This machine can only run BATCH jobs until you fix it; buyers' apps (Jupyter, vLLM,\n"
          f"  llama.cpp, game servers) will not be placed here. Fix: {npol.hint(reason)}")


def main(argv):
    # The agent service can't see /root/.docker (ProtectHome) and never inherits DOCKER_HOST (the
    # drop-in unsets it), so check docker exactly as it will see it: the default local context.
    os.environ.pop("DOCKER_HOST", None)
    os.environ["DOCKER_CONTEXT"] = "default"
    cmd = argv[0] if argv else ""
    if cmd == "repair":
        return 0 if repair(argv[argv.index("--auto") + 1] if "--auto" in argv else None) else 1
    if cmd == "selftest":
        ok, reason = selftest()
        report(ok, reason)
        return 0 if ok else 1
    print("usage: isolation.py repair [--auto VERSION] | selftest")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
