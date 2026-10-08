"""gpu_driver.py — OPT-IN NVIDIA driver updates on Ubuntu / Pop!_OS nodes, applied only while idle.

update.sh runs `gpu_driver.py upgrade` as root after gpu_toolkit.py. The seller opts in per node on
the Petabyte dashboard ("may update the NVIDIA driver and reboot this machine when no rental is
running"); the agent mirrors that choice into OPTIN. A driver change needs a reboot: the new user-space
libraries don't match the loaded kernel module until then, so CUDA is down in between. With consent:

  1. the host must be Ubuntu or Pop!_OS with an apt-installed driver; never WSL (WSL uses the
     Windows driver), never Secure Boot (a new module needs MOK enrollment at the console), never a
     .run-installed driver;
  2. the target is the distro's current driver (Pop!_OS: system76-driver-nvidia; Ubuntu:
     ubuntu-drivers' recommended nvidia-driver-*); nothing happens if the installed one is current;
  3. the node drains (the agent stops claiming jobs) and it proceeds only when no Petabyte buyer
     container is running, otherwise it un-drains and retries on the next run;
  4. `apt-get -s` must show it removes nothing but NVIDIA driver packages;
  5. after installing, the kernel module built for the RUNNING kernel must be the new version, or the
     previous driver is reinstalled and the machine is NOT rebooted;
  6. reboot with a one-minute warning; systemd brings the agent back.
A failed target is not retried until the distro offers a different one. Stdlib only.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time

STATE_DIR = "/var/lib/petabyte-agent"
STATE = f"{STATE_DIR}/gpu_driver.json"
OPTIN = f"{STATE_DIR}/driver_update_optin"       # the agent writes it from the seller's setting
DRAIN = f"{STATE_DIR}/drain"                     # the agent claims no new job while it exists
BUSY = f"{STATE_DIR}/busy.json"                  # the agent's live tasks, rewritten every heartbeat
DRAIN_WAIT_S = 45                                # > 2 heartbeats (15 s), so BUSY is fresh after it
NVIDIA_PKG = re.compile(r"^(nvidia-|libnvidia-|xserver-xorg-video-nvidia|linux-(modules|objects|signatures)"
                        r"-nvidia|system76-driver-nvidia|screen-resolution-extra|libxnvctrl)")
_ENV = dict(os.environ, DEBIAN_FRONTEND="noninteractive")


def _run(cmd, timeout=900):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=_ENV)


def _out(cmd, timeout=60):
    try:
        return _run(cmd, timeout).stdout
    except Exception:                                    # noqa: BLE001 — a probe, never fatal
        return ""


def _newer(a, b):
    return _run(["dpkg", "--compare-versions", a, "gt", b], timeout=10).returncode == 0


def distro():
    try:
        with open("/etc/os-release") as f:
            return dict(ln.strip().split("=", 1) for ln in f if "=" in ln).get("ID", "").strip('"')
    except OSError:
        return ""


def is_wsl():
    try:
        with open("/proc/version") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


EFI = "/sys/firmware/efi"


def secure_boot():
    """Fail-safe: on unless mokutil says off, or there is no UEFI (legacy BIOS has no Secure Boot)."""
    sb = _out(["mokutil", "--sb-state"]).lower()
    if "secureboot disabled" in sb or "doesn't support secure boot" in sb:
        return False
    if "secureboot enabled" in sb:
        return True
    if not os.path.isdir(EFI):
        return False
    try:
        var = next(n for n in os.listdir(f"{EFI}/efivars") if n.startswith("SecureBoot-"))
        with open(f"{EFI}/efivars/{var}", "rb") as f:
            return f.read()[-1:] != b"\x00"
    except (OSError, StopIteration):
        return True                                      # unknown: assume on


def installed():
    """(package, version) of the apt-installed nvidia-driver-* package, or (None, None)."""
    for ln in _out(["dpkg-query", "-W", "-f=${Package} ${Version} ${Status}\n", "nvidia-driver-*"]).splitlines():
        p = ln.split()
        if len(p) >= 5 and p[0].startswith("nvidia-driver-") and p[-1] == "installed":
            return p[0], p[1]
    return None, None


def target(dist):
    """(install-package, driver-package, candidate version) the distro offers now, or Nones."""
    if dist == "pop":
        dep = re.search(r"Depends:\s*(nvidia-driver-\S+)", _out(["apt-cache", "depends", "system76-driver-nvidia"]))
        pkg, inst = (dep.group(1) if dep else None), "system76-driver-nvidia"
    else:
        rec = re.search(r"driver\s*:\s*(nvidia-driver-\S+).*recommended", _out(["ubuntu-drivers", "devices"], 120))
        pkg = inst = rec.group(1) if rec else None
    if not pkg:
        return None, None, None
    cand = re.search(r"Candidate:\s*(\S+)", _out(["apt-cache", "policy", pkg]))
    return inst, pkg, (cand.group(1) if cand and cand.group(1) != "(none)" else None)


def module_version():
    """Version of the nvidia kernel module built for the RUNNING kernel ('570.181'), or ''."""
    kernel = os.uname().release
    return _out(["modinfo", "-k", kernel, "-F", "version", "nvidia"]).strip()


def busy(since):
    """Unless the agent reported AFTER `since` that it runs no task (render/batch jobs carry no
    container label, so its own list is the source of truth), or a pb-* container still runs."""
    try:
        with open(BUSY) as f:
            b = json.load(f)
    except (OSError, ValueError):
        return True                                  # no fresh report: assume busy
    if b.get("at", 0) < since or b.get("tasks"):
        return True
    return any(n.startswith("pb-") for n in _out(["docker", "ps", "--format", "{{.Names}}"]).split())


def why_not():
    if sys.platform != "linux":
        return "not Linux"
    if getattr(os, "geteuid", lambda: -1)() != 0:
        return "must run as root"
    if not os.path.exists(OPTIN):
        return "the seller has not turned on automatic driver updates for this node"
    if is_wsl():
        return ("WSL uses the Windows NVIDIA driver: update it in Windows (NVIDIA App or "
                "GeForce Experience); a Linux driver must never be installed inside WSL")
    if distro() not in ("ubuntu", "pop"):
        return "automatic driver updates support Ubuntu and Pop!_OS only"
    if not shutil.which("nvidia-smi") or not installed()[0]:
        return "no apt-installed NVIDIA driver (a .run-installed driver is left alone)"
    if secure_boot():
        return "Secure Boot is on: a new driver module needs key enrollment at the console"
    return None


def _simulate_ok(inst):
    sim = _run(["apt-get", "-s", "install", "-y", inst], timeout=300)
    if sim.returncode != 0:
        return False, "apt cannot install it: " + (sim.stderr or sim.stdout).strip()[-200:]
    removed = [ln.split()[1] for ln in sim.stdout.splitlines() if ln.startswith("Remv ")]
    bad = [p for p in removed if not NVIDIA_PKG.match(p)]
    return (False, "it would remove non-NVIDIA packages: " + ", ".join(bad[:5])) if bad else (True, None)


def upgrade():
    """One opt-in, idle-only driver update attempt. Records {before, target, ok, reason, rebooting}."""
    try:
        os.unlink(DRAIN)                                 # left by an interrupted run: never strand the node
    except OSError:
        pass
    last, reason, rebooting = _load(), why_not(), False
    old_pkg, old_ver = installed()
    st = {"at": int(time.time()), "before": old_ver, "target": None, "ok": False}
    if reason is None:
        _run(["apt-get", "update"], timeout=600)
        inst, pkg, want = target(distro())
        st["target"] = want
        if not want:
            reason = "the distro offers no NVIDIA driver for this GPU"
        elif not _newer(want, old_ver):                  # current, or a newer driver the seller chose
            st["ok"], reason = True, None
        elif last.get("target") == want and last.get("failed"):
            st["failed"] = True
            reason = f"driver {want} failed before; waiting for a newer one"
        else:
            reason, rebooting = _apply(inst, want, old_pkg, old_ver, st)
    if rebooting:
        r = _run(["shutdown", "-r", "+1", "Petabyte: rebooting to finish an NVIDIA driver update"], timeout=30)
        if r.returncode != 0:
            _run(["systemctl", "start", "petabyte-agent"], timeout=120)
            rebooting, reason = False, "the driver is installed but the reboot could not be scheduled: reboot this machine"
    st.update(reason=reason, rebooting=rebooting)
    _save(st)
    print("gpu driver: " + json.dumps(st))
    return st["ok"]


def _apply(inst, want, old_pkg, old_ver, st):
    """Drain, install, verify, or roll back. -> (reason or None, reboot?)."""
    if busy(time.time() - 90):
        return "a buyer job or rental is running; will retry when the node is idle", False
    t0 = time.time()
    open(DRAIN, "w").close()                                       # the agent stops claiming jobs
    try:
        time.sleep(DRAIN_WAIT_S)
        if busy(t0):
            return "a job started while draining; will retry when the node is idle", False
        ok, why = _simulate_ok(inst)
        if not ok:
            st["failed"] = True
            return why, False
        _run(["systemctl", "stop", "petabyte-agent"], timeout=120)
        r = _run(["apt-get", "install", "-y", "-o", "DPkg::Lock::Timeout=300",
                  "-o", "Dpkg::Options::=--force-confold", inst], timeout=3600)
        upstream = want.split("-")[0].split(":")[-1]
        if r.returncode == 0 and module_version().startswith(upstream):
            st["ok"] = True
            return None, True
        # Never reboot into a driver whose module isn't there: put the previous driver back.
        st["failed"] = True
        rb = _run(["apt-get", "install", "-y", "--allow-downgrades", "-o", "Dpkg::Options::=--force-confold",
                   f"{old_pkg}={old_ver}"], timeout=3600)
        _run(["systemctl", "start", "petabyte-agent"], timeout=120)
        why = ("the new driver's kernel module was not built for this kernel" if r.returncode == 0
               else "install failed: " + (r.stderr or r.stdout).strip()[-200:])
        if rb.returncode != 0 or not module_version().startswith(old_ver.split("-")[0].split(":")[-1]):
            return why + "; ROLLBACK FAILED: reinstall the NVIDIA driver on this machine before rebooting", False
        return why + "; the previous driver was reinstalled", False
    finally:
        try:
            os.unlink(DRAIN)
        except OSError:
            pass


def _load():
    try:
        with open(STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(st):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(STATE, "w") as f:
            json.dump(st, f)
        os.chmod(STATE, 0o644)                                     # the agent reports it
    except OSError as e:
        print(f"gpu driver: could not record the result ({e})")


def main(argv):
    if argv[:1] == ["upgrade"]:
        return 0 if upgrade() else 1
    print("usage: gpu_driver.py upgrade")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
