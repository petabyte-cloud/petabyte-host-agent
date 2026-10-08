"""gpu_toolkit.py — keep the NVIDIA Container Toolkit current on every NVIDIA node (apt hosts).

update.sh runs `gpu_toolkit.py upgrade --auto <bundle-sha>` as root after each signed agent update,
at most once per bundle. An old toolkit mounts only part of a newer driver: 1.12.1 on the RTX 2060
(driver 550.67) left out libnvidia-gpucomp, so every EEVEE render segfaulted (2026-10-07).

What it changes, and what it never does:
  * upgrades ONLY the toolkit packages, to the newest version in NVIDIA's own apt repo, requested by
    exact version so a distro pin (Pop!_OS prefers System76's older build) can't hold it back;
  * never restarts Docker: running rentals keep their containers, new containers use the new hook;
  * never touches the GPU driver: a driver change needs a reboot and can break the seller's desktop.
Non-apt hosts and hosts without the toolkit are left alone, with the reason recorded.

Stdlib only: runs with the agent venv's python, but needs nothing from it.
"""
import functools
import json
import os
import re
import shutil
import subprocess
import sys
import time

STATE = "/var/lib/petabyte-agent/gpu_toolkit.json"
LIST = "/etc/apt/sources.list.d/nvidia-container-toolkit.list"
KEYRING = "/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg"
REPO = "https://nvidia.github.io/libnvidia-container"
PKGS = ("nvidia-container-toolkit", "nvidia-container-toolkit-base",
        "libnvidia-container-tools", "libnvidia-container1")


def _run(cmd, timeout=900):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _newer(a, b):
    """Debian version order (revisions, ~ prereleases) — dpkg's own comparison."""
    return _run(["dpkg", "--compare-versions", a, "gt", b], timeout=10).returncode == 0


def package_version():
    """The installed nvidia-container-toolkit package version ('1.12.1-0pop1~…'), or None."""
    r = _run(["dpkg-query", "-W", "-f=${Version}", "nvidia-container-toolkit"], timeout=10)
    return (r.stdout.strip() or None) if r.returncode == 0 else None


def installed():
    """The toolkit version the hooks use ('1.12.1'), or None."""
    try:
        out = _run(["nvidia-container-cli", "--version"], timeout=10).stdout
    except Exception:                                    # noqa: BLE001 — not installed / broken
        return None
    m = re.search(r"cli-version:\s*([\d.]+)", out or "")
    return m.group(1) if m else None


def newest_from_nvidia():
    """The newest nvidia-container-toolkit version in NVIDIA's repo ('1.17.8-1'), or None."""
    out = _run(["apt-cache", "madison", "nvidia-container-toolkit"], timeout=60).stdout or ""
    vs = [ln.split("|")[1].strip() for ln in out.splitlines()
          if ln.count("|") >= 2 and "nvidia.github.io" in ln.split("|")[2]]
    return max(vs, key=functools.cmp_to_key(lambda a, b: 1 if _newer(a, b) else -1 if _newer(b, a) else 0)) \
        if vs else None


def why_not():
    """None if this host can be upgraded here, else why it is left alone."""
    if sys.platform != "linux":
        return "not Linux"
    if getattr(os, "geteuid", lambda: -1)() != 0:
        return "must run as root"
    if not shutil.which("nvidia-smi"):
        return "no NVIDIA GPU"
    if not (shutil.which("apt-get") and shutil.which("apt-cache")):
        return "not an apt host: update nvidia-container-toolkit with this system's package manager"
    if not installed():
        return "nvidia-container-toolkit is not installed: re-run the Petabyte installer"
    return None


def _ensure_repo():
    """NVIDIA's apt source, exactly as install.sh writes it (older installs may lack it)."""
    if os.path.exists(LIST):
        return True
    tmp = LIST + ".tmp"
    key = _run(["bash", "-o", "pipefail", "-c",
                f"curl -fsSL {REPO}/gpgkey | gpg --batch --yes --dearmor -o {KEYRING}"])
    lst = _run(["bash", "-o", "pipefail", "-c", f"curl -fsSL {REPO}/stable/deb/nvidia-container-toolkit.list"
                f" | sed 's#deb https://#deb [signed-by={KEYRING}] https://#g' > {tmp}"])
    try:
        with open(tmp) as f:
            good = key.returncode == 0 and lst.returncode == 0 and "deb [signed-by=" in f.read()
    except OSError:
        good = False
    if good:
        os.replace(tmp, LIST)          # only a verified list: an empty one would read as "present"
    else:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return good


def upgrade(bundle=None):
    """Upgrade if NVIDIA's repo has a newer toolkit. Records {bundle, before, after, ok, reason}."""
    last = _load()
    if bundle and last.get("bundle") == bundle:
        print("gpu toolkit: already checked for this agent version; skipping")
        return bool(last.get("ok"))
    before, reason, ok = installed(), why_not(), False
    if reason is None:
        if not _ensure_repo():
            reason = "could not add NVIDIA's apt repository"
        else:
            # refresh only NVIDIA's source: fast, and a broken third-party repo can't block it
            upd = _run(["apt-get", "update", "-o", f"Dir::Etc::sourcelist={LIST}",
                        "-o", "Dir::Etc::sourceparts=-", "-o", "APT::Get::List-Cleanup=0"])
            want = newest_from_nvidia() if upd.returncode == 0 else None
            have = package_version()
            if upd.returncode != 0:                      # never decide from a stale index
                reason = "refreshing NVIDIA's apt index failed: " + (upd.stderr or "").strip()[-200:]
            elif not want:
                reason = "NVIDIA's repository lists no toolkit for this system"
            elif have and not _newer(want, have):
                ok = True                                # already current
            else:
                r = _run(["apt-get", "install", "-y", "-o", "DPkg::Lock::Timeout=300",
                          "-o", "Dpkg::Options::=--force-confold", *(f"{p}={want}" for p in PKGS)],
                         timeout=1800)
                ok = r.returncode == 0
                reason = None if ok else ((r.stderr or r.stdout).strip()[-300:] or "apt-get failed")
    state = {"bundle": bundle, "at": int(time.time()), "before": before, "after": installed(),
             "ok": ok, "reason": reason}
    _save(state)
    print("gpu toolkit: " + json.dumps(state))
    return ok


def _load():
    try:
        with open(STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(state):
    try:
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        with open(STATE, "w") as f:
            json.dump(state, f)
    except OSError as e:
        print(f"gpu toolkit: could not record the result ({e})")


def main(argv):
    if argv[:1] == ["upgrade"]:
        return 0 if upgrade(argv[argv.index("--auto") + 1] if "--auto" in argv else None) else 1
    print("usage: gpu_toolkit.py upgrade [--auto BUNDLE_SHA]")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
