"""Buyer image snapshots: bake a rental's container into S3, and launch a rental from one.

Bake  : docker commit (every platform-injected env var and pb.* label scrubbed, CMD reset to the
        base image's) -> `docker save` streamed through sha256 into presigned UploadPart URLs, one
        part in memory at a time (never the whole tar on disk) -> report etags + sha256 + image id
        -> remove the committed image. Failures go to the buyer's job log and /jobs/snapshot_failed.
Launch: stream the tar to disk under a size cap -> verify size + sha256 against what the API
        recorded -> `docker load` -> verify the loaded image id -> tag pbsnap/<id>. The caller then
        runs it like any template (--pull=never, the normal isolation flags).

The node never holds object-storage credentials: every URL is minted per part / per object by the
API for this node's own task. See docs/IMAGE_SNAPSHOTS.md.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile

import httpx

PART_BYTES = 64 * 1024 * 1024
_ID = re.compile(r"^[0-9a-f]{16,32}$")
_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
_LABEL_KEY = re.compile(r"^pb\.[A-Za-z0-9_.-]+$")
# Always emptied, even when the image itself set them (a token baked into a shared image is still
# a token). Everything else is emptied only when the running container's value differs from the
# base image's, i.e. when the platform (or the buyer's launch) injected it.
_SENSITIVE = re.compile(r"TOKEN|PASSWORD|PASSWD|SECRET|CREDENTIAL|^PETABYTE_", re.I)
_TMP_DIR = "/var/lib/petabyte/snapshots"     # on disk, never /tmp (often a RAM tmpfs)


def _docker(*args, timeout=120):
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout,
                          check=True).stdout.strip()


def _env(items):
    return dict(x.split("=", 1) for x in (items or []) if "=" in x)


def commit_argv(container, info, base):
    """`docker commit` argv for a rental container (info = docker inspect of it, base = docker
    image inspect of the image it was started from)."""
    cfg, base_cfg = info.get("Config") or {}, base.get("Config") or {}
    image_env = _env(base_cfg.get("Env"))
    scrub = sorted(k for k, v in _env(cfg.get("Env")).items()
                   if image_env.get(k) != v or _SENSITIVE.search(k))
    labels = sorted(k for k in (cfg.get("Labels") or {}) if k.startswith("pb."))
    bad = [k for k in scrub if not _ENV_KEY.match(k)] + [k for k in labels if not _LABEL_KEY.match(k)]
    if bad:
        raise ValueError("container has an environment variable or label that cannot be scrubbed safely")
    argv = ["docker", "commit"]
    for k in scrub:
        argv += ["--change", f"ENV {k}="]
    if labels:
        argv += ["--change", "LABEL " + " ".join(f'{k}=""' for k in labels)]
    if cfg.get("Cmd") != base_cfg.get("Cmd"):            # drop this rental's run args (model etc.)
        argv += ["--change", "CMD " + json.dumps(base_cfg.get("Cmd") or [])]
    return argv + [container]


def _part_etag(post, put, sid, tid, n, chunk, attempts=3):
    last = None
    for _ in range(attempts):
        try:
            grant = post("/jobs/snapshot_part_url", {"task_id": tid, "snapshot_id": sid, "part_number": n})
            grant.raise_for_status()
            r = put(grant.json()["upload_url"], content=chunk, timeout=900, trust_env=False)
            r.raise_for_status()
            etag = r.headers.get("ETag")
            if etag:
                return etag
            last = ValueError("storage returned no ETag for a part")
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (404, 409, 413):  # cancelled/deleted/over the limit: stop
                raise ValueError(f"upload refused ({e.response.status_code})") from None
            last = e
        except httpx.HTTPError as e:
            last = e
    raise last


def upload(image, sid, tid, post, put=None, part_size=PART_BYTES, max_bytes=30 * 1024 ** 3):
    """Stream `docker save <image>` into presigned parts. Returns (size, sha256 hex, parts)."""
    digest, size, parts = hashlib.sha256(), 0, []
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(["docker", "save", image], stdout=subprocess.PIPE, stderr=err)
        try:
            while True:
                chunk = proc.stdout.read(part_size)       # blocks until a full part or EOF
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError(f"image is larger than the {max_bytes / 1024 ** 3:g} GB snapshot limit")
                digest.update(chunk)
                n = len(parts) + 1
                parts.append({"part_number": n, "etag": _part_etag(post, put or httpx.put, sid, tid, n, chunk)})
            if proc.wait(timeout=120):
                err.seek(0)
                raise ValueError("docker save failed: " + err.read().decode(errors="replace")[-200:])
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
    if not parts:
        raise ValueError("docker save produced no data")
    return size, digest.hexdigest(), parts


def bake(entry, post, log, put=None):
    """Snapshot the running container of task entry['task_id'] (heartbeat `snapshots` entry)."""
    sid, tid, image = str(entry.get("id") or ""), int(entry.get("task_id") or 0), None
    try:
        if not _ID.match(sid):
            raise ValueError("invalid snapshot id")
        ids = _docker("ps", "-q", "--filter", f"label=pb.task={tid}",
                      "--filter", "label=pb.kind=template").split()
        if len(ids) != 1:
            raise ValueError("the rental's container is not running")
        info = json.loads(_docker("inspect", ids[0]))[0]
        base = json.loads(_docker("image", "inspect", info["Image"]))[0]
        log(tid, f"snapshot {sid}: saving the container (platform secrets scrubbed)")
        image = _docker(*commit_argv(ids[0], info, base)[1:], timeout=3600)
        if not _IMAGE_ID.match(image):
            raise ValueError("docker commit returned no image id")
        size, sha, parts = upload(image, sid, tid, post, put,
                                  part_size=int(entry.get("part_size") or PART_BYTES),
                                  max_bytes=int(entry.get("max_bytes") or 30 * 1024 ** 3))
        done = post("/jobs/snapshot_complete", {"task_id": tid, "snapshot_id": sid, "parts": parts,
                                                "sha256": sha, "size_bytes": size, "image_id": image})
        done.raise_for_status()
        log(tid, f"snapshot {sid} saved ({size / 1024 ** 3:.2f} GB, sha256 {sha[:12]})")
    except Exception as e:                               # noqa: BLE001
        # Our own ValueErrors are buyer-safe; anything else may carry a presigned URL.
        reason = str(e) if isinstance(e, ValueError) else type(e).__name__
        log(tid, f"SNAPSHOT {sid} FAILED ({reason}); nothing was saved")
        try:
            post("/jobs/snapshot_failed", {"task_id": tid, "snapshot_id": sid, "reason": reason[:300]})
        except Exception:                                # noqa: BLE001 — the API expires it anyway
            pass
    finally:
        if image:
            subprocess.run(["docker", "image", "rm", image], capture_output=True, timeout=300)


def load(snap, task_id, stream=None):
    """Download, verify and `docker load` the snapshot dispatched with a task; return its tag."""
    sid, image_id, sha = str(snap.get("id") or ""), str(snap.get("image_id") or ""), str(snap.get("sha256") or "")
    size = int(snap.get("size") or 0)
    if not (_ID.match(sid) and _IMAGE_ID.match(image_id) and re.fullmatch(r"[0-9a-f]{64}", sha) and size > 0):
        raise ValueError("snapshot record is incomplete")
    tag = f"pbsnap/{sid}"
    try:
        if json.loads(_docker("image", "inspect", tag))[0]["Id"] == image_id:
            return tag                                   # loaded + verified on an earlier launch
    except (subprocess.CalledProcessError, ValueError, KeyError, IndexError):
        pass
    import template_storage
    os.makedirs(_TMP_DIR, mode=0o700, exist_ok=True)
    if shutil.disk_usage(_TMP_DIR).free < 2 * size + template_storage.policy()[1]:
        raise ValueError("not enough free disk on this host to load the snapshot")
    fd, path = tempfile.mkstemp(prefix="pbsnap-", suffix=".tar", dir=_TMP_DIR)
    try:
        digest, got = hashlib.sha256(), 0
        with os.fdopen(fd, "wb") as out, (stream or httpx.stream)("GET", snap["url"], timeout=120, trust_env=False) as r:
            r.raise_for_status()
            for chunk in r.iter_bytes(1024 * 1024):
                got += len(chunk)
                if got > size:
                    raise ValueError("snapshot download is larger than recorded")
                digest.update(chunk)
                out.write(chunk)
        if got != size or digest.hexdigest() != sha:
            raise ValueError("snapshot integrity check failed (sha256 mismatch); refusing to load it")
        loaded = re.findall(r"Loaded image ID: (sha256:[0-9a-f]{64})", _docker("load", "-i", path, timeout=3600))
        if loaded != [image_id]:
            for other in loaded:
                subprocess.run(["docker", "image", "rm", other], capture_output=True, timeout=300)
            raise ValueError("loaded image id does not match the snapshot; refusing to run it")
        _docker("tag", image_id, tag)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    try:                                                 # seller cache budget may reclaim it later
        with template_storage.locked():
            state = template_storage.read()
            state["images"][image_id] = {"ref": tag, "bytes": size, "used_at": __import__("time").time(),
                                         "task_id": task_id}
            template_storage.save(state)
    except Exception:                                    # noqa: BLE001 — ledger is housekeeping only
        pass
    return tag
