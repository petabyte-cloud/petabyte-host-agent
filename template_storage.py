"""Seller-controlled image admission and an ownership ledger. Never prune shared Docker.

The agent separately applies Docker writable-layer quotas. Image budgets and named-volume checks
here are admission/runtime guards, not filesystem quotas: Docker and other applications can write
concurrently. Model/work volumes remain private to each rental.
"""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import threading
import time

_LOCK = threading.RLock()
_CATALOG = ()
STATE = Path("/var/lib/petabyte/images.json")
GIB = 1024 ** 3


def docker(*args, timeout=30):
    return subprocess.run(["docker", *args], capture_output=True, text=True,
                          timeout=timeout, check=True).stdout.strip()


_PULLS = {}   # image -> docker pull that outlived its caller's wait. Only touched under locked().


def _reap_pulls():
    """Close background pulls that SUCCEEDED (no zombie or open log per image). A failed one stays
    until its image is asked for again, so that caller gets the real Docker error instead of a
    fresh pull that hides it behind more timeouts. Called under locked()."""
    for image, proc in list(_PULLS.items()):
        if proc.poll() == 0:
            _PULLS.pop(image, None)
            proc.pb_log.close()


def _pull(image, timeout):
    """`docker pull` that is NOT killed when the caller stops waiting. Docker cancels a pull whose
    client goes away and discards the half-downloaded layer, so on a link that needs longer than one
    wait for the biggest layer the image could never arrive: spec 269 (RTX 3090) timed out every
    hourly 120 s template probe on 2026-10-04/05. The pull keeps going after TimeoutExpired; the next
    prepare joins it (or finds the image). Output goes to a file, so a full pipe never stalls it.
    Must run under locked() (prepare does), which serialises _PULLS."""
    import tempfile
    proc = _PULLS.get(image)
    if proc is None:                                # a finished one is consumed below: its result
        log = tempfile.TemporaryFile()
        proc = subprocess.Popen(["docker", "pull", "--", image], stdout=log, stderr=subprocess.STDOUT)
        proc.pb_log = log
        _PULLS[image] = proc
    rc = proc.wait(timeout=timeout)                 # TimeoutExpired: still downloading, left running
    _PULLS.pop(image, None)
    try:
        proc.pb_log.seek(0)
        out = proc.pb_log.read().decode("utf-8", errors="replace")[-4000:]
    finally:
        proc.pb_log.close()
    if rc != 0:
        raise subprocess.CalledProcessError(rc, ["docker", "pull", image], output=out)


@contextlib.contextmanager
def locked():
    import fcntl
    with _LOCK:
        STATE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_fd = os.open(STATE.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(lock_fd, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield


def read():
    if not STATE.exists():
        return {"images": {}, "events": []}
    # Corruption fails closed: never overwrite ownership evidence with an empty ledger.
    return json.loads(STATE.read_text())


def save(state):
    tmp = STATE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(json.dumps(state))
    tmp.replace(STATE)


def policy():
    return (max(0, int(os.getenv("PB_IMAGE_CACHE_GB", "30"))) * GIB,
            max(0, int(os.getenv("PB_DISK_RESERVE_GB", "10"))) * GIB)


def inspect(image):
    try:
        return json.loads(docker("image", "inspect", image))[0]
    except subprocess.CalledProcessError:
        return None


def free_bytes():
    root = docker("info", "--format", "{{.DockerRootDir}}")
    return shutil.disk_usage(root).free


def collect(state, *, all_owned=False):
    budget, reserve = policy()
    present = set(docker("image", "ls", "-aq", "--no-trunc").splitlines())
    for image_id, entry in sorted(list(state["images"].items()),
                                  key=lambda item: item[1]["used_at"]):
        if image_id not in present:
            state["images"].pop(image_id)
            continue
        size = sum(x["bytes"] for x in state["images"].values())
        if not all_owned and size <= budget and free_bytes() >= reserve:
            break
        # Include stopped containers belonging to ANY application. Never force deletion.
        if docker("ps", "-aq", "--filter", "ancestor=" + image_id):
            continue
        try:
            docker("image", "rm", image_id)
        except subprocess.CalledProcessError:
            # Multiple tags / a concurrent consumer: Docker's own guard wins.
            continue
        state["images"].pop(image_id)


def prepare(image, task_id, *, cached_only=False, timeout=900):
    """Return cached/downloaded; caller must run with --pull=never after this check."""
    with locked():
        _reap_pulls()
        state = read()
        collect(state)
        save(state)
        if free_bytes() < policy()[1]:
            raise RuntimeError("Seller free disk reserve prevents launch")
        existing = inspect(image)
        outcome = "cached"
        pending = state.setdefault("pending", {})   # image -> image IDs present before its pull began
        if existing is None:
            if cached_only:
                raise RuntimeError("Buyer selected cached image only; image is not cached")
            budget, reserve = policy()
            if budget == 0 or free_bytes() < reserve:
                raise RuntimeError("Seller image budget/free disk reserve prevents download")
            # Check ALL pre-existing IDs, not just this tag. Another tag can share an image. A pull an
            # earlier call left running keeps that call's snapshot, so ownership is still exact.
            if image not in pending:
                pending[image] = sorted(docker("image", "ls", "-aq", "--no-trunc").splitlines())
                save(state)
            _pull(image, timeout=max(1, min(int(timeout), 3600)))   # TimeoutExpired: keeps downloading
            existing = inspect(image)
            if existing is None:
                raise RuntimeError("Downloaded image is unavailable")
        if image in pending:                        # pulled now, or by a pull an earlier call left running
            before = set(pending.pop(image))
            image_id = existing["Id"]
            budget, reserve = policy()
            if image_id not in before:
                state["images"][image_id] = {"ref": image, "bytes": int(existing["Size"]),
                                              "used_at": time.time(), "task_id": task_id}
            outcome = "downloaded"
            # Persist ownership before an admission refusal, so uninstall can clean it.
            save(state)
            if sum(x["bytes"] for x in state["images"].values()) > budget or free_bytes() < reserve:
                collect(state)
                save(state)
                if (inspect(image) is None or sum(x["bytes"] for x in state["images"].values()) > budget
                        or free_bytes() < reserve):
                    raise RuntimeError("Downloaded image exceeded seller cache budget/free disk reserve")
        if existing["Id"] in state["images"]:
            state["images"][existing["Id"]]["used_at"] = time.time()
        state.setdefault("seen", {})[image] = existing["Id"]
        state["events"] = (state["events"] + [{"task_id": task_id, "image": image,
                            "outcome": outcome, "at": time.time()}])[-100:]
        save(state)
        return outcome


def job_violation(volume=None):
    """A soft runtime guard, not a filesystem quota. Never inspect foreign volumes."""
    if free_bytes() < policy()[1]:
        return "Seller free disk reserve reached"
    if volume:
        info = json.loads(docker("volume", "inspect", volume))[0]
        if not (info.get("Labels") or {}).get("pb.task"):
            raise RuntimeError("Refusing to inspect an unowned job volume")
        used = int(subprocess.run(["du", "-sb", "--", info["Mountpoint"]],
                   capture_output=True, text=True, timeout=10, check=True).stdout.split()[0])
        budget = max(0, int(os.getenv("PB_JOB_DATA_GB", "20"))) * GIB
        if used > budget:
            return "Seller per-rental data budget exceeded"
    return None


def set_catalog(images):
    global _CATALOG
    if isinstance(images, list) and len(images) <= 64:
        _CATALOG = tuple(x for x in images if isinstance(x, str) and 0 < len(x) <= 2048)


def report(gateway=""):
    """Bounded, public cache metadata; private rental activity stays on this seller."""
    try:
        with locked():
            state = read()
            budget, reserve = policy()
            images = []
            for ref in list(dict.fromkeys([*_CATALOG, *state.get("seen", {})]))[:64]:
                current = inspect(ref)
                if current:
                    images.append(hashlib.sha256(ref.encode()).hexdigest())
            result = {"version": 1, "probe_version": 1, "port_bridge_version": 1, "native_udp_version": 1,
                      "minecraft_status_version": 1, "ssh_start_version": 1,
                      "images": images, "cache_budget_bytes": int(budget),
                      "cache_bytes": sum(x["bytes"] for x in state["images"].values()),
                      "disk_free_bytes": free_bytes(), "disk_reserve_bytes": int(reserve)}
        import egress_vpn
        try:
            result["native_udp_ready"] = bool(egress_vpn.enabled() and egress_vpn.ensure_tunnel()
                                               and egress_vpn.peer_ready())
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            result["native_udp_ready"] = False
        if gateway:
            try:
                host = gateway.rsplit("@", 1)[-1]
                start = time.monotonic()
                with socket.create_connection((host, 22), timeout=2):
                    result["gateway_tcp_ms"] = round((time.monotonic() - start) * 1000, 1)
            except OSError:
                pass  # Cache metadata remains useful when the gateway probe fails.
        return result
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


_SNAPSHOT = (0, None)
_PROBE = None


def heartbeat_report(gateway=""):
    # Docker can take minutes or hang. Never let storage probes block liveness.
    global _PROBE
    if _PROBE is None or not _PROBE.is_alive():
        def refresh():
            global _SNAPSHOT
            _SNAPSHOT = (time.monotonic(), report(gateway))
        _PROBE = threading.Thread(target=refresh, daemon=True)
        _PROBE.start()
    at, value = _SNAPSHOT
    return value if time.monotonic() - at < 45 else None


def uninstall():
    """Run only after the agent stops. Remove its labeled resources and owned images."""
    for kind, listing, removal in (("container", ("ps", "-aq"), ("rm", "-f")),
                                   ("volume", ("volume", "ls", "-q"), ("volume", "rm")),
                                   ("network", ("network", "ls", "-q"), ("network", "rm"))):
        for resource in docker(*listing, "--filter", "label=pb.task").splitlines():
            docker(*removal, resource)
    with locked():
        state = read()
        collect(state, all_owned=True)
        save(state)
        if state["images"]:
            raise RuntimeError("Some Petabyte images are in use or have shared tags; ownership ledger retained")


if __name__ == "__main__":
    import sys
    if sys.argv[1:] == ["uninstall"]:
        uninstall()
    elif len(sys.argv) == 3 and sys.argv[1] == "prepare":
        prepare(sys.argv[2], 0)
    else:
        with locked():
            state = read()
            state["policy"] = {"cache_budget_bytes": int(policy()[0]), "disk_reserve_bytes": int(policy()[1])}
            print(json.dumps(state, indent=2))
