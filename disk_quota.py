"""Per-container writable-layer quota (`docker run --storage-opt size=…`), only where Docker can enforce it.

Docker enforces `--storage-opt size` only on quota-capable backends (overlay2 on xfs+pquota, btrfs,
zfs, devicemapper). On the common seller hosts — ext4, WSL2, the containerd image store — the daemon
REJECTS the flag ("--storage-opt is supported only for overlay over xfs with 'pquota' mount option"),
so passing it unconditionally (#695) failed every rental on those hosts. We probe once per agent
process and keep the quota wherever it works; elsewhere the host disk stays the bound, as before #695.
"""
import logging
import os
import subprocess

_supported = None          # None = not probed yet; probe again on the next launch after an inconclusive try


def job_disk_limit():
    """AGENT_JOB_DISK_GB (default 20) in Docker syntax; a bad value is a config error, not a silent default."""
    raw = os.getenv("AGENT_JOB_DISK_GB", "").strip() or "20"
    try:
        size = int(raw, 10)
    except (TypeError, ValueError):
        raise RuntimeError("AGENT_JOB_DISK_GB must be an integer from 1 to 1024") from None
    if not 1 <= size <= 1024:
        raise RuntimeError("AGENT_JOB_DISK_GB must be an integer from 1 to 1024")
    return f"{size}G"


def flags():
    """`["--storage-opt", "size=NG"]` when this host's Docker enforces it, else `[]`."""
    global _supported
    quota = ["--storage-opt", f"size={job_disk_limit()}"]
    if _supported is None:
        try:
            images = subprocess.run(["docker", "images", "-q"], capture_output=True, text=True,
                                    timeout=30).stdout.split()
            if not images:
                return quota                      # nothing local to probe with: keep the quota
            # create (never start) a throwaway container; the daemon validates storage-opt here
            r = subprocess.run(["docker", "create", *quota, images[0], "true"], capture_output=True,
                               text=True, timeout=60)
        except Exception:                         # noqa: BLE001 — inconclusive: keep the quota, re-probe
            return quota
        if r.returncode == 0:
            subprocess.run(["docker", "rm", "-f", r.stdout.strip()], capture_output=True, timeout=30)
            _supported = True
        elif "storage-opt" in (r.stderr or ""):
            _supported = False
            logging.warning("Docker on this host cannot enforce --storage-opt (needs overlay2 on xfs with "
                            "pquota, btrfs or zfs): buyer containers run without a writable-layer quota; "
                            "the host disk is the bound")
        else:
            return quota                          # unrelated failure: keep the quota, re-probe next time
    return quota if _supported else []


if __name__ == "__main__":                        # self-check with a fake docker
    import types
    calls = []

    def fake(stderr, rc):
        def run(cmd, **k):
            calls.append(cmd)
            if cmd[1] == "images":
                return types.SimpleNamespace(stdout="abc123\n", stderr="", returncode=0)
            if cmd[1] == "create":
                return types.SimpleNamespace(stdout="cid\n", stderr=stderr, returncode=rc)
            return types.SimpleNamespace(stdout="", stderr="", returncode=0)
        return run

    subprocess.run = fake("", 0)
    assert flags() == ["--storage-opt", "size=20G"] and ["docker", "rm", "-f", "cid"] in calls
    _supported = None
    subprocess.run = fake("Error response from daemon: --storage-opt is supported only for overlay over "
                          "xfs with 'pquota' mount option.", 125)
    assert flags() == [] and flags() == []        # cached: no second probe
    assert sum(c[1] == "create" for c in calls) == 2
    _supported = None
    subprocess.run = fake("Cannot connect to the Docker daemon", 1)
    assert flags() == ["--storage-opt", "size=20G"] and _supported is None
    os.environ["AGENT_JOB_DISK_GB"] = "0"
    try:
        flags()
        raise AssertionError("bad AGENT_JOB_DISK_GB accepted")
    except RuntimeError:
        pass
    print("disk_quota ok")
