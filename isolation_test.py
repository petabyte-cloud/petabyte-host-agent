"""isolation_test.py — the one check+repair that installer, auto-update and agent share.

A Windows seller's WSL distro had Docker Desktop's CLI, so install.sh skipped Docker Engine and every
networked rental on the node was refused (spec 45). isolation.py now decides, per case, whether a
repair is SAFE (install/enable the native Engine, install iptables, unset an inherited DOCKER_HOST)
or must be left alone (Docker Desktop, a foreign daemon, a shared WSL network) — and never touches
Docker Desktop. The auto-update runs it at most once per agent version.

Offline: subprocess, which, realpath and the self-test are stubbed. Run: python isolation_test.py
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import isolation as iso
import network_policy as npol

_tmp = tempfile.mkdtemp(prefix="pb-isolation-")
iso.STATE = os.path.join(_tmp, "isolation.json")
iso.AGENT_ENV = os.path.join(_tmp, "agent.env")
iso.DROPIN = os.path.join(_tmp, "petabyte-agent.service.d", "10-local-docker.conf")
HOST = "node1"
_fail = 0


def ok(label, cond, extra=""):
    global _fail
    print(("ok   " if cond else "FAIL ") + label + (f"   [{extra}]" if extra and not cond else ""))
    if not cond:
        _fail += 1


def facts(**kw):
    base = {"linux": True, "root": True, "wsl": False, "dockerd": True, "systemd": True,
            "missing": [], "redirect": False, "desktop": False, "bridge_taken": False,
            "daemon": {"name": HOST, "os": "linux"}}
    base.update(kw)
    return base


# ------------------------------------------------------------------ 1. the decision
with patch.object(iso.socket, "gethostname", lambda: HOST):
    P = iso.plan
    ok("native local Engine, tools present -> nothing to do", P(facts()) == ([], None))
    ok("not Linux -> left alone", P(facts(linux=False)) == ([], "a local Linux firewall is required"))
    ok("not root -> left alone", P(facts(root=False))[0] == [])
    ok("Docker Desktop -> left alone, even with tools missing and no daemon",
       P(facts(desktop=True, daemon=None, missing=["iptables"])) == ([], npol.DESKTOP))
    ok("a daemon that isn't this host's -> left alone",
       P(facts(daemon={"name": "elsewhere", "os": "linux"})) == ([], "container daemon and firewall must share the host"))
    ok("Engine installed but stopped (e.g. rootless setup disabled it) -> enable",
       P(facts(daemon=None)) == (["enable"], None))
    ok("no Engine at all -> install", P(facts(daemon=None, dockerd=False)) == (["install"], None))
    ok("WSL + a docker0 some other distro's Engine owns -> left alone (shared network namespace)",
       P(facts(daemon=None, wsl=True, bridge_taken=True))[0] == [])
    ok("no systemd -> left alone", P(facts(daemon=None, systemd=False))[0] == [])
    ok("iptables/ip missing -> install the tools", P(facts(missing=["ip6tables"])) == (["tools"], None))
    ok("agent.env redirects DOCKER_HOST -> drop-in", P(facts(redirect=True)) == (["dropin"], None))
    ok("all three, in a safe order", P(facts(daemon=None, dockerd=False, missing=["ip"], redirect=True))
       == (["install", "tools", "dropin"], None))


# ------------------------------------------------------------------ 2. inspect() recognises the cases
def fake_run(info=None, rc=0):
    def run(args, **kw):
        if args[:2] == ["docker", "info"]:
            return subprocess.CompletedProcess(args, rc, json.dumps(info or {}), "")
        raise AssertionError(f"inspect() must not run {args}")
    return run


def look(which, realpath=lambda p: p, info=None, rc=0):
    with patch.object(iso.shutil, "which", which), \
         patch.object(iso.os.path, "realpath", realpath), \
         patch.object(iso.subprocess, "run", fake_run(info, rc)), \
         patch.object(iso, "_is_wsl", lambda: True):
        return iso.inspect()


tools = lambda t: f"/usr/bin/{t}"
dd_link = {"/var/run/docker.sock": "/mnt/wsl/docker-desktop/shared-sockets/guest-services/docker.proxy.sock",
           "/usr/bin/docker": "/mnt/wsl/docker-desktop/cli-tools/usr/bin/docker"}
f = look(tools, lambda p: dd_link.get(p, p), rc=1)
ok("Docker Desktop WSL integration is recognised from its symlinks even while it is stopped", f["desktop"])
f = look(tools, info={"Name": "docker-desktop", "OSType": "linux", "OperatingSystem": "Docker Desktop"})
ok("…and from its daemon", f["desktop"] and f["daemon"]["name"] == "docker-desktop")
f = look(tools, info={"Name": HOST, "OSType": "linux", "OperatingSystem": "Ubuntu 24.04"})
ok("a native Engine is not mistaken for Docker Desktop", not f["desktop"] and f["daemon"]["name"] == HOST)
f = look(lambda t: None if t in ("docker", "dockerd") else f"/usr/bin/{t}")
ok("no docker CLI -> no daemon, and docker is never run", f["daemon"] is None and not f["dockerd"])

Path(iso.AGENT_ENV).write_text("PETABYTE_SPEC_ID=4\nDOCKER_HOST=tcp://10.0.0.5:2375\n")
ok("DOCKER_HOST in agent.env is a redirect", iso._env_redirect())
Path(iso.AGENT_ENV).write_text("PETABYTE_SPEC_ID=4\n")
ok("a clean agent.env is not", not iso._env_redirect())

# ------------------------------------------------------------------ 3. repair(): only safe steps, then self-test
ran = []


def run_ok(args, **kw):
    ran.append(list(args))
    return subprocess.CompletedProcess(args, 0, "", "")


def repair(f, selftest=(True, None), run=run_ok, version=None):
    ran.clear()
    with patch.object(iso, "inspect", lambda: f), \
         patch.object(iso, "selftest", lambda: selftest), \
         patch.object(iso.subprocess, "run", run), \
         patch.object(iso.socket, "gethostname", lambda: HOST):
        return iso.repair(version)


def state():
    return iso.last_repair()


Path(iso.STATE).unlink(missing_ok=True)
r = repair(facts(desktop=True, daemon=None), selftest=(False, npol.DESKTOP))
ok("Docker Desktop: runs NOTHING on the host (no install, no restart)", ran == [], str(ran))
ok("…records {ok: false, reason, repaired: false} for the heartbeat",
   not r and state()["reason"] == npol.DESKTOP and state()["repaired"] is False)

r = repair(facts(daemon=None, dockerd=False))
ok("no Engine: get.docker.com, then enable the Engine, then restart the agent",
   ran == [["sh", "-c", "curl -fsSL https://get.docker.com | sh"],
           ["systemctl", "enable", "--now", "docker"],
           ["systemctl", "try-restart", "petabyte-agent"]], str(ran))
ok("…self-tested and recorded repaired", r and state()["ok"] and state()["repaired"]
   and state()["actions"] == ["install"])


def run_fail_install(args, **kw):
    ran.append(list(args))
    return subprocess.CompletedProcess(args, 1 if args[0] == "sh" else 0, "", "")


r = repair(facts(daemon=None, dockerd=False), selftest=(False, "job network policy unavailable"), run=run_fail_install)
ok("a failed install stops there: the Engine is not enabled, nothing half-applied",
   ["systemctl", "enable", "--now", "docker"] not in ran, str(ran))
ok("…and the failure is the recorded reason", state()["reason"] == "isolation repair step 'install' failed"
   and state()["ok"] is False)

r = repair(facts(redirect=True))
dropin = Path(iso.DROPIN)
ok("inherited DOCKER_HOST + a verified native Engine -> drop-in unsets it for the agent",
   dropin.exists() and "UnsetEnvironment=DOCKER_HOST DOCKER_CONTEXT" in dropin.read_text()
   and ["systemctl", "daemon-reload"] in ran)
dropin.unlink()
repair(facts(redirect=True, daemon=None),     # plans enable + dropin; the Engine never comes up
       selftest=(False, "job network policy unavailable: `docker info --format` failed"))
ok("…never when nothing native serves the local socket (checked again at apply time)",
   not dropin.exists() and state()["reason"] == "isolation repair step 'dropin' failed", str(state()))

# ------------------------------------------------------------------ 4. once per agent version
Path(iso.STATE).unlink(missing_ok=True)
seen = []


def run_seen(args, **kw):
    seen.append(list(args))
    return subprocess.CompletedProcess(args, 0, "", "")


repair(facts(daemon=None), run=run_seen, version="bundle-v1")
first = list(seen)
seen.clear()
repair(facts(daemon=None), run=run_seen, version="bundle-v1")
ok("the auto-update repairs at most once per agent version", first and seen == [], str(seen))
repair(facts(daemon=None), run=run_seen, version="bundle-v2")
ok("…and tries again for the next version", ["systemctl", "enable", "--now", "docker"] in seen)
ok("the installer (no version) always runs", repair(facts()) is True)


def interrupted(action):
    raise KeyboardInterrupt


Path(iso.STATE).unlink(missing_ok=True)
with patch.object(iso, "_apply", interrupted):
    try:
        repair(facts(daemon=None), version="bundle-v3")
    except KeyboardInterrupt:
        pass
ok("the attempt is recorded BEFORE acting, so a crash mid-repair isn't retried every 6 h",
   state().get("version") == "bundle-v3" and state().get("reason") == "interrupted")

# ------------------------------------------------------------------ 5. the self-test builds and removes a real network
calls = []
with patch.object(npol, "probe", lambda: (True, None)), \
     patch.object(npol, "ensure", lambda tid: calls.append(("ensure", tid)) or f"pb-net-t{tid}"), \
     patch.object(npol, "cleanup", lambda tid: calls.append(("cleanup", tid))), \
     patch.object(iso.subprocess, "run", lambda a, **k: calls.append(tuple(a))):
    res = iso.selftest()
ok("self-test: ensure(probe id) then removes its network and chains",
   res == (True, None) and calls[0] == ("ensure", iso.SELFTEST_TID)
   and ("docker", "network", "rm", f"pb-net-t{iso.SELFTEST_TID}") in calls
   and ("cleanup", iso.SELFTEST_TID) in calls, str(calls))
calls.clear()


def refuse(tid):
    raise npol.NetworkUnavailable("job firewall differs from required policy")


with patch.object(npol, "probe", lambda: (True, None)), patch.object(npol, "ensure", refuse), \
     patch.object(npol, "cleanup", lambda tid: calls.append(("cleanup", tid))), \
     patch.object(iso.subprocess, "run", lambda a, **k: calls.append(tuple(a))):
    res = iso.selftest()
ok("a refused network is a FAIL with its reason, and is still cleaned up",
   res == (False, "job firewall differs from required policy") and ("cleanup", iso.SELFTEST_TID) in calls)
with patch.object(npol, "probe", lambda: (False, npol.DESKTOP)), \
     patch.object(npol, "ensure", lambda tid: calls.append("ensure-ran")):
    ok("a failing probe short-circuits (nothing is created)", iso.selftest() == (False, npol.DESKTOP)
       and "ensure-ran" not in calls)

os.environ["DOCKER_HOST"] = "tcp://10.0.0.5:2375"
with patch.object(iso, "selftest", lambda: (True, None)):
    iso.main(["selftest"])
ok("the CLI checks docker exactly as the agent service sees it (no DOCKER_HOST, default context)",
   "DOCKER_HOST" not in os.environ and os.environ.get("DOCKER_CONTEXT") == "default")

# ------------------------------------------------------------------ 6. the installer, updater and Windows installer use it
sh = (HERE / "install.sh").read_text()
ok("install.sh no longer skips the Engine just because a `docker` CLI exists",
   "command -v docker >/dev/null || curl" not in sh and "get.docker.com" not in sh)
ok("install.sh fetches the agent, then runs the shared repair at its Docker step (before the GPU toolkit)",
   sh.index('cp -r "$TMP/lumaris_agent/."') < sh.index('isolation.py" repair') < sh.index("nvidia-container-toolkit"))
ok("install.sh self-tests BEFORE it lists the node, and says 'batch jobs' on FAIL",
   sh.index("isolation.py selftest") < sh.index("provision.py") and "only run BATCH jobs" in sh)
ok("install.sh keeps a migrated node's registration (PETABYTE_KEEP_SPEC)", "PETABYTE_KEEP_SPEC" in sh)
up = (HERE / "update.sh").read_text()
ok("update.sh runs the repair once per signed bundle, on BOTH the updated and up-to-date runs",
   'isolation.py" repair --auto "$BUNDLE_SHA"' in up
   and up.index('isolation.py" repair --auto') > up.index('echo "already up to date"'))
ok("update.sh records the verified bundle it applied (the agent reports it), after the signature check",
   up.index("SECURITY: agent bundle signature did not verify") < up.index('> "$STATE/bundle.sha256"')
   < up.index('isolation.py" repair --auto'))
ok("install.sh records the bundle it installed, and forgets it for a git/local install",
   "rm -f /var/lib/petabyte-agent/bundle.sha256" in sh
   and '| cut -d\' \' -f1 > /var/lib/petabyte-agent/bundle.sha256' in sh)
for script in ("install.sh", "update.sh"):
    rc = subprocess.run(["bash", "-n", str(HERE / script)]).returncode
    ok(f"{script} parses", rc == 0)
ps = (HERE / "install.ps1").read_text()
ok("install.ps1 keeps the PC awake by DEFAULT (no answer / Enter / no terminal = on)",
   "else { $true }" in ps and "[Y/n; 15s timeout]" in ps and "[y/N" not in ps)
ok("install.ps1 reuses only an EXPLICIT earlier keep-awake choice and records it",
   "$prev.keepAwakeChosen -eq $true" in ps and "keepAwakeChosen = [bool]$keepAwakeChosen" in ps
   and '"false","0","no","n")) { $keepAwake = $false; $keepAwakeChosen = $true }' in ps)
ok("install.ps1 gives the agent its own distro, imported from a SHA-256-checked Ubuntu image",
   '$Distro = "Petabyte"' in ps and "wsl.exe --import" in ps and "Get-FileHash" in ps
   and "wsl.exe --install -d" not in ps)
ok("install.ps1 moves an existing node only when Docker Desktop serves its distro, keeping its registration",
   '"docker-desktop|Docker Desktop"' in ps and "PETABYTE_KEEP_SPEC=1" in ps and "KeepOld" in ps)
# Reinstall on a PC that already has a node = repair it (same listing), never a duplicate.
ok("install.ps1 re-running on an existing node keeps its registration unless PETABYTE_NEW_NODE=1",
   '$env:PETABYTE_NEW_NODE -ne "1"' in ps and "PETABYTE_SPEC_ID=" in ps
   and ps.count('$keep = "export PETABYTE_KEEP_SPEC=1"') == 2 and "Repairing it in place" in ps)
ok("install.sh hands PETABYTE_KEEP_SPEC to provision.py (which re-attests the kept spec)",
   'PETABYTE_KEEP_SPEC="${PETABYTE_KEEP_SPEC:-}"' in sh and sh.count(".venv/bin/python provision.py") == 1)
# The logon task's window IS the node: it must say so, not be a blank wsl.exe tab sellers close.
ka = (HERE / "keepalive.sh").read_text()
_task = 'bash /opt/petabyte-agent/keepalive.sh || exec sleep infinity'
ok("the logon task runs keepalive.sh (bare keep-alive fallback), not a blank `sleep infinity`",
   _task in ps and '--exec sleep infinity"' not in ps)
ok("keepalive.sh titles its window, says keep-open in English AND Arabic, and keeps running",
   "\\033]0;Petabyte node - keep open\\007" in ka and "keep this window open" in ka
   and "خلّ هذي النافذة مفتوحة" in ka and "while :; do" in ka and "manage.ps1" in ka)
ok("keepalive.sh parses", subprocess.run(["bash", "-n", str(HERE / "keepalive.sh")]).returncode == 0)


def _ka_status(docker_out):
    """First status line keepalive.sh prints, with fake systemctl (agent active) + docker on PATH."""
    with tempfile.TemporaryDirectory() as d:
        for name, body in (("systemctl", "echo active"), ("docker", f"printf '{docker_out}'")):
            p = os.path.join(d, name)
            open(p, "w").write(f"#!/bin/sh\n{body}\n"); os.chmod(p, 0o755)
        r = subprocess.run(["timeout", "3", "bash", str(HERE / "keepalive.sh")], capture_output=True,
                           env={**os.environ, "PATH": d + ":" + os.environ.get("PATH", "")})
        return r.stdout.decode("utf-8", "replace").rsplit("Status", 1)[-1]


_rented, _idle = _ka_status("abc123\\n"), _ka_status("")
ok("keepalive status says RENTED / earning (English + Arabic) while a rental container runs",
   "RENTED - earning now" in _rented and "مؤجّر ويكسب الحين" in _rented, _rented)
ok("keepalive status says online - waiting for rentals when nothing is rented",
   "online - waiting for rentals" in _idle and "RENTED" not in _idle, _idle)
mg = (HERE / "manage.ps1").read_text()
ok("manage.ps1 with no action offers a menu whose default (Enter / 1) is Resume",
   '"" { "resume" } "1" { "resume" }' in mg and "1) Resume" in mg)
ok("manage.ps1 resume moves an older install's blank-window task onto keepalive.sh", _task in mg)

print(f"\n{'ALL PASS' if _fail == 0 else f'{_fail} FAILED'}")
sys.exit(1 if _fail else 0)
