"""Bounded filesystem checkpoints for event-driven agents.

The platform owns checkpoint authorization/encryption; a worker only receives the
checkpoint for its current assignment. No RAM or container image is checkpointed.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import math
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path, PurePosixPath

import httpx

MAX_STATE_BYTES = 16 * 1024 * 1024
MAX_FILES = 4096
MAX_TAR_BYTES = MAX_STATE_BYTES + MAX_FILES * 1024 + 10240
MAX_EVENT_BYTES = 1024 * 1024


class StateError(ValueError):
    """A checkpoint could not be safely restored or durably persisted."""


def pack_state(directory):
    """Archive only regular files/directories, rejecting links and oversized state."""
    directory = Path(directory)
    raw = io.BytesIO()
    count = total = 0
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for parent, directories, files in os.walk(directory, followlinks=False):
            directories.sort()
            files.sort()
            for name in directories + files:
                path = Path(parent) / name
                info = path.lstat()
                count += 1
                if count > MAX_FILES:
                    raise StateError("checkpoint contains too many entries")
                if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                    raise StateError("checkpoint contains a link or special file")
                item = tarfile.TarInfo(path.relative_to(directory).as_posix())
                if "\\" in item.name or ":" in item.name:
                    raise StateError("checkpoint contains a nonportable entry name")
                item.mode = 0o755 if stat.S_ISDIR(info.st_mode) else 0o644
                if stat.S_ISDIR(info.st_mode):
                    item.type = tarfile.DIRTYPE
                    archive.addfile(item)
                else:
                    total += info.st_size
                    if total > MAX_STATE_BYTES:
                        raise StateError("checkpoint exceeds state size limit")
                    item.size = info.st_size
                    # The container has stopped; O_NOFOLLOW also rejects a leaf
                    # swapped to a symlink before it is opened.
                    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                    with os.fdopen(fd, "rb") as handle:
                        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                            raise StateError("checkpoint contains a special file")
                        archive.addfile(item, handle)
    if raw.tell() > MAX_TAR_BYTES:
        raise StateError("checkpoint archive exceeds size limit")
    payload = gzip.compress(raw.getvalue(), mtime=0)
    if len(payload) > MAX_STATE_BYTES:
        raise StateError("compressed checkpoint exceeds size limit")
    return payload


def restore_state(payload, directory, expected_sha256):
    """Validate the complete archive before writing into a fresh private workspace."""
    if len(payload) > MAX_STATE_BYTES:
        raise StateError("compressed checkpoint exceeds size limit")
    if not expected_sha256 or hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise StateError("checkpoint integrity verification failed")
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(payload)) as compressed:
            raw = compressed.read(MAX_TAR_BYTES + 1)
        if len(raw) > MAX_TAR_BYTES:
            raise StateError("checkpoint archive exceeds size limit")
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
            members = archive.getmembers()
            if len(members) > MAX_FILES:
                raise StateError("checkpoint contains too many entries")
            total = 0
            seen = set()
            for member in members:
                path = PurePosixPath(member.name)
                if (not member.name or path.is_absolute() or "\\" in member.name
                        or any(part in ("", ".", "..") for part in member.name.split("/"))
                        or ":" in member.name or path.as_posix() in seen
                        or not (member.isfile() or member.isdir())):
                    raise StateError("checkpoint contains an unsafe entry")
                seen.add(path.as_posix())
                total += member.size
                if member.size < 0 or total > MAX_STATE_BYTES:
                    raise StateError("checkpoint exceeds state size limit")
            root = Path(directory)
            if any(root.iterdir()):
                raise StateError("restore requires an empty workspace")
            for member in members:
                target = root.joinpath(*PurePosixPath(member.name).parts)
                target.parent.mkdir(mode=0o777, parents=True, exist_ok=True)
                if member.isdir():
                    target.mkdir(mode=0o777, exist_ok=True)
                    target.chmod(0o777)
                else:
                    with archive.extractfile(member) as source, target.open("xb") as output:
                        shutil.copyfileobj(source, output, length=64 * 1024)
                    # The private outer directory prevents host access; the
                    # mounted leaf must remain writable by arbitrary image UIDs.
                    target.chmod(0o666)
    except (tarfile.TarError, EOFError, OSError) as error:
        raise StateError("invalid checkpoint archive") from error


def _download_state(url, headers):
    payload = bytearray()
    with httpx.stream("GET", url, headers=headers, timeout=60, trust_env=False) as response:
        response.raise_for_status()
        for chunk in response.iter_bytes():
            payload.extend(chunk)
            if len(payload) > MAX_STATE_BYTES:
                raise StateError("compressed checkpoint exceeds size limit")
    return bytes(payload)


def _upload_state(url, headers, payload):
    response = httpx.post(url, headers={**headers, "Content-Type": "application/gzip"},
                          content=payload, timeout=120, trust_env=False)
    response.raise_for_status()
    result = response.json()
    if result.get("sha256", result.get("content_hash")) != hashlib.sha256(payload).hexdigest():
        raise StateError("checkpoint persistence was not acknowledged")


def stop_container(name):
    """Require teardown before checkpointing, so state cannot change under the archive."""
    response = subprocess.run(["docker", "rm", "-f", name], capture_output=True,
                              text=True, timeout=30, check=False)
    if response.returncode and "No such container" not in response.stderr:
        raise StateError("agent container teardown failed")


def cold_start_seconds(name, invocation_started):
    """Observe Docker's actual process start, including preparation/restore/pull time.

    Missing daemon evidence and clock skew remain unknown rather than zero. This
    is worker-reported telemetry, not hardware attestation or proof of execution.
    """
    try:
        response = subprocess.run(["docker", "inspect", "--format", "{{.State.StartedAt}}", name],
                                  capture_output=True, text=True, timeout=5, check=False)
        if response.returncode:
            return None
        # Python 3.9's fromisoformat does not accept the Z suffix.
        started = datetime.fromisoformat(response.stdout.strip().replace("Z", "+00:00"))  # noqa: FURB162
        elapsed = started.timestamp() - invocation_started
        return elapsed if math.isfinite(elapsed) and 0 <= elapsed <= 86400 else None
    except (OSError, ValueError, OverflowError, subprocess.TimeoutExpired):
        return None


def execute(task, api_url, headers, build_command, run_command,
            *, download=_download_state, upload=_upload_state, stop=stop_container,
            measure=cold_start_seconds):
    """Restore, execute one finite event, persist, and remove all execution resources."""
    invocation_started = time.time()
    agent = task["agent"]
    runtime = int(task.get("max_runtime_s") or 0)
    if not 1 <= runtime <= 86400:
        raise StateError("agent execution requires a finite runtime limit")
    event = json.dumps(agent.get("trigger_payload"), ensure_ascii=False).encode("utf-8")
    if len(event) > MAX_EVENT_BYTES:
        raise StateError("invocation exceeds event size limit")
    url = f"{api_url.rstrip('/')}/api/agents/worker/{int(task['task_id'])}/state"
    # Reuse the existing startup orphan sweep's pb-task- prefix. A worker
    # restart must remove an execution that outlived its timeout watchdog.
    name = "pb-task-agent-" + uuid.uuid4().hex[:16]
    try:
        with tempfile.TemporaryDirectory(prefix="pb-agent-") as temporary:
            root = Path(temporary)
            state = root / "state"
            state.mkdir(mode=0o777)
            state.chmod(0o777)
            event_path = root / "event.json"
            event_path.write_bytes(event)
            event_path.chmod(0o444)
            if agent.get("has_checkpoint"):
                restore_state(download(url, headers), state, agent.get("checkpoint_sha256"))
            argv = build_command(task, name=name)
            # Add platform-controlled mounts before the image. The builder's
            # initial argv is always docker run --rm, so these are Docker flags.
            argv[3:3] = ["--mount", f"type=bind,src={state},dst=/state",
                         "--mount", f"type=bind,src={event_path},dst=/petabyte/event.json,readonly",
                         "--workdir", "/state"]
            # Retain the exited container only long enough to inspect its real
            # process start. Mandatory teardown still precedes checkpointing.
            if argv[2] != "--rm":
                raise StateError("unexpected container command format")
            del argv[2]
            cold_start = None
            try:
                code, output, _truncated, timed_out, overflowed = run_command(
                    argv, name, timeout_s=runtime)
                cold_start = measure(name, invocation_started)
            finally:
                stop(name)
            if timed_out:
                raise StateError("agent execution exceeded its runtime limit")
            if overflowed:
                raise StateError("agent execution exceeded its output limit")
            if code:
                raise StateError("agent container exited unsuccessfully")
            upload_headers = dict(headers)
            if cold_start is not None and math.isfinite(cold_start) and 0 <= cold_start <= 86400:
                upload_headers["X-Agent-Cold-Start-Seconds"] = str(cold_start)
            upload(url, upload_headers, pack_state(state))
            return output
    finally:
        path = task.pop("_env_file", None)
        if path:
            Path(path).unlink(missing_ok=True)
