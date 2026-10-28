"""Qualified Lucebox adapter. Inputs come from the platform's pinned profile manifest.

Hardware reports are software evidence from the enrolled seller agent, not a TEE claim.
Only one physical NVIDIA GPU is supported initially; never combine unrelated GPUs.
"""
from __future__ import annotations

import csv
import errno
import hashlib
import io
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import safe_fetch
import template_storage

_PREPARING = {}
_PREPARING_SINCE = {}
_LOCK = threading.Lock()
_CAPS = [0.0, None]
_UUID = re.compile(r"GPU-[0-9a-fA-F-]{32,40}\Z")


def manifest():
    return json.loads(Path(__file__).with_name("lucebox_profiles.json").read_text(encoding="utf-8-sig"))


def capabilities(refresh=False):
    """Fresh bounded hardware evidence, or None when any required probe is unavailable."""
    now = time.monotonic()
    if not refresh and now - _CAPS[0] < 30:
        return _CAPS[1]
    answer = None
    try:
        if platform.system() != "Linux" or platform.machine().lower() not in ("x86_64", "amd64"):
            raise ValueError("linux/amd64 required")
        out = subprocess.run([
            "nvidia-smi", "--query-gpu=uuid,compute_cap,driver_version,memory.total,memory.free,mig.mode.current",
            "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5, check=True).stdout
        if len(out) > 4096:
            raise ValueError("oversized GPU inventory")
        rows = list(csv.reader(io.StringIO(out)))
        if len(rows) != 1 or len(rows[0]) != 6:
            raise ValueError("one physical GPU required")
        uuid, cc, driver, total, free, mig_mode = (part.strip() for part in rows[0])
        if not _UUID.fullmatch(uuid) or mig_mode.lower() not in ("disabled", "[n/a]", "n/a", "not supported"):
            raise ValueError("MIG is unavailable")
        mem = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
        answer = dict(version=1, backend="cuda", cpu_arch="x86_64", gpu_count=1, gpu_uuid=uuid,
                      mig_mode="disabled",
                      compute_capability=cc, driver=driver, vram_mib=int(float(total)),
                      free_vram_mib=int(float(free)),
                      ram_available_mib=int(mem["MemAvailable"].split()[0]) // 1024,
                      disk_free_bytes=int(template_storage.free_bytes()))
    except (OSError, ValueError, OverflowError, KeyError, subprocess.SubprocessError):
        pass
    _CAPS[:] = [now, answer]
    return answer


def _version(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", value):
        raise ValueError("Lucebox: invalid NVIDIA driver evidence")
    return tuple(int(v) for v in value.split("."))


def configuration(task, evidence=None, *, check_disk=True):
    """Validate dispatch again on the host; public parameters can never become runtime flags."""
    spec = manifest()
    request = task.get("lucebox")
    if (not isinstance(request, dict) or set(request) != {"profile", "context_length", "concurrency", "gpu_uuid"}
            or task.get("mig") or task.get("image") != spec["image"]
            or task.get("cache") != spec["mount_path"] or task.get("gpu") is not True
            or task.get("snapshot") or task.get("secrets_sealed")):
        raise ValueError("Lucebox: invalid pinned runtime assignment")
    profile = spec["profiles"].get(request.get("profile"))
    if profile is None:
        raise ValueError("Lucebox: unsupported model/profile; private model credentials are unavailable")
    context, concurrency = request.get("context_length"), request.get("concurrency")
    if (type(context) is not int or not profile["min_context"] <= context <= profile["max_context"]
            or type(concurrency) is not int or concurrency != 1):
        raise ValueError("Lucebox: context or concurrency exceeds the qualified profile")
    evidence = capabilities(refresh=True) if evidence is None else evidence
    if (not evidence or evidence.get("version") != 1 or evidence.get("backend") != "cuda"
            or evidence.get("cpu_arch") not in ("amd64", "x86_64") or evidence.get("gpu_count") != 1
            or evidence.get("mig_mode") != "disabled"
            or evidence.get("gpu_uuid") != request["gpu_uuid"]
            or not _UUID.fullmatch(str(request["gpu_uuid"]))):
        raise ValueError("Lucebox: assigned single NVIDIA GPU is unavailable; no CPU/AMD/MIG fallback")
    if (evidence.get("compute_capability") not in spec["compute_capabilities"]
            or _version(evidence.get("driver")) < _version(spec["min_driver"])):
        raise ValueError("Lucebox: unsupported GPU architecture or NVIDIA driver")
    for field, minimum in (("vram_mib", profile["min_vram_mib"]),
                           ("free_vram_mib", profile["min_vram_mib"]),
                           ("ram_available_mib", profile["min_ram_mib"]),
                           ("disk_free_bytes", profile["min_disk_bytes"])):
        if field == "disk_free_bytes" and not check_disk:
            continue  # The image and model are already stored at the pre-start recheck.
        if type(evidence.get(field)) is not int or evidence[field] < minimum:
            raise ValueError(f"Lucebox: insufficient {field}; need {minimum} for weights, KV cache and runtime overhead")
    filename = profile["filename"]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}\.gguf", filename):
        raise ValueError("Lucebox: invalid curated model path")
    url = urlsplit(profile["url"])
    if (url.scheme != "https" or url.hostname != "huggingface.co" or url.port not in (None, 443)
            or url.username or url.password or url.query or url.fragment
            or not re.fullmatch(r"/[-A-Za-z0-9_.]+/[-A-Za-z0-9_.]+/resolve/[a-f0-9]{40}/[A-Za-z0-9._-]+\.gguf", url.path)
            or not re.fullmatch(r"[a-f0-9]{64}", profile["sha256"])):
        raise ValueError("Lucebox: model source is not a pinned public Hugging Face artifact")
    target = spec["mount_path"] + "/" + filename
    key = (task.get("env") or {}).get("PETABYTE_LUCEBOX_KEY")
    if not isinstance(key, str) or not 32 <= len(key) <= 256 or not key.isascii() or any(ord(c) <= 32 for c in key):
        raise ValueError("Lucebox: endpoint authentication credential is missing")
    gate = spec["mount_path"] + "/.petabyte-lucebox-gate.py"
    return dict(profile=profile, target=target, gate=gate,
                gpu_args=["--gpus", "device=" + request["gpu_uuid"]],
                env={"LUCE_TARGET": target, "LUCE_DRAFT": "none", "PETABYTE_LUCEBOX_KEY": key,
                     "PETABYTE_LUCEBOX_CONTEXT": str(context), "PETABYTE_LUCEBOX_MODEL": profile["model"]},
                args=[gate])


def cancel(task_id):
    try:
        task_id = int(task_id)
    except (ValueError, TypeError):
        return
    with _LOCK:
        event = _PREPARING.get(task_id)
        if event is not None:
            event.set()


def begin(task_id):
    with _LOCK:
        _PREPARING[task_id] = threading.Event()
        _PREPARING_SINCE[task_id] = time.monotonic()


def finish(task_id):
    with _LOCK:
        _PREPARING.pop(task_id, None)
        _PREPARING_SINCE.pop(task_id, None)


def cancelled(task_id):
    with _LOCK:
        event = _PREPARING.get(task_id)
        return bool(event and event.is_set())


def cancel_inactive(active_task_ids):
    """The authoritative heartbeat can cancel downloads before Docker starts the container.

    A 30s grace covers the heartbeat/claim race, matching the existing orphan-reap convention.
    Missing heartbeat data never cancels a rental.
    """
    if active_task_ids is None:
        return []
    active, now = {str(tid) for tid in active_task_ids}, time.monotonic()
    with _LOCK:
        ended = [tid for tid, event in _PREPARING.items()
                 if str(tid) not in active and now - _PREPARING_SINCE.get(tid, now) >= 30]
        for tid in ended:
            _PREPARING[tid].set()
    return ended


def prepare_image(task):
    """Wait cancellably on Petabyte's image cache. Its existing verified pull may finish warm."""
    result, done = [], threading.Event()

    def prepare():
        try:
            result.append(template_storage.prepare(task["image"], task["task_id"],
                cached_only=bool(task.get("params", {}).get("cached_image_only", False)), timeout=900))
        except Exception as error:
            result.append(error)
        finally:
            done.set()

    threading.Thread(target=prepare, daemon=True, name=f"pb-luce-image-{task['task_id']}").start()
    deadline = time.monotonic() + 900
    while not done.wait(0.25):
        if cancelled(task["task_id"]):
            raise RuntimeError("Lucebox: image preparation cancelled")
        if time.monotonic() >= deadline:
            raise RuntimeError("Lucebox: image download timed out after 900 seconds")
    if cancelled(task["task_id"]):
        raise RuntimeError("Lucebox: image preparation cancelled")
    if isinstance(result[0], Exception):
        raise result[0]
    return result[0]


class _Sink:
    def __init__(self, handle, stop, task_id, size, progress):
        self.handle, self.stop, self.task_id, self.size, self.progress = handle, stop, task_id, size, progress
        self.digest, self.received, self.last = hashlib.sha256(), 0, -1

    def write(self, block):
        if self.stop.is_set():
            raise RuntimeError("Lucebox: model preparation cancelled")
        self.received += len(block)
        self.digest.update(block)
        try:
            self.handle.write(block)
        except OSError as error:
            if error.errno == errno.ENOSPC:
                raise RuntimeError("Lucebox: insufficient storage during model download") from None
            raise
        pct = min(99, self.received * 100 // self.size)
        if pct // 10 > self.last:
            self.last = pct // 10
            self.progress(self.task_id, 25 + pct // 2, f"Lucebox: model download {pct}% (checksum pending)")


def prepare_model(task, name, config, progress, timeout=900):
    """Download outside the container, verify SHA-256, then copy into its private named volume.

    safe_fetch pins public DNS for every redirect and uses no host proxies/credentials. The
    runtime container is still CREATED, so it cannot load partial bytes or serve unmetered work.
    Temporary files and cancellation state are removed on every exit.
    """
    tid, profile = task["task_id"], config["profile"]
    with _LOCK:
        owned = tid not in _PREPARING
        stop = _PREPARING.setdefault(tid, threading.Event())
    try:
        state = subprocess.run(["docker", "inspect", "-f", "{{.State.Status}}", name],
                               capture_output=True, text=True, timeout=5, check=True).stdout.strip()
        if stop.is_set() or state != "created":
            raise RuntimeError("Lucebox: model preparation cancelled or its private container was lost")
        if profile["size_bytes"] > safe_fetch.SINK_MAX_BYTES:
            raise ValueError("Lucebox: model exceeds the bounded download limit")
        if shutil.disk_usage(tempfile.gettempdir()).free < profile["size_bytes"] + 128 * 1024 ** 2:
            raise RuntimeError("Lucebox: insufficient temporary disk space for the checked model download")
        progress(tid, 25, "Lucebox: downloading the pinned model into this rental's private volume")
        with tempfile.TemporaryDirectory(prefix="pb-lucebox-") as directory:
            path = Path(directory) / "model.gguf"
            with path.open("wb") as handle:
                sink = _Sink(handle, stop, tid, profile["size_bytes"], progress)
                response = safe_fetch.get(profile["url"], timeout=timeout, max_bytes=profile["size_bytes"],
                                          sink=sink, cancel_event=stop)
                if response.status_code in (401, 403):
                    raise RuntimeError("Lucebox: model download denied; this profile requires public ungated weights")
                response.raise_for_status()
            if stop.is_set():
                raise RuntimeError("Lucebox: model preparation cancelled")
            if sink.received != profile["size_bytes"] or sink.digest.hexdigest() != profile["sha256"]:
                raise RuntimeError("Lucebox: incomplete model or SHA-256 mismatch; refusing to load")
            subprocess.run(["docker", "cp", str(path), name + ":" + config["target"]],
                           capture_output=True, text=True, timeout=180, check=True)
            if stop.is_set():
                raise RuntimeError("Lucebox: model preparation cancelled")
            progress(tid, 80, "Lucebox: model checksum verified; loading on the assigned CUDA GPU")
    except safe_fetch.FetchDenied:
        raise RuntimeError("Lucebox: model download failed, exceeded its deadline or used an unsafe destination") from None
    finally:
        if owned:
            finish(tid)


def ready(host_port, key, model="qwen3-0.6b-bf16"):
    """Only a loaded matching model which completes a token through its authenticated gate is ready."""
    import httpx
    url = f"http://127.0.0.1:{int(host_port)}"
    headers = {"Authorization": "Bearer " + key}
    response = httpx.get(url + "/v1/models", headers=headers, timeout=3, trust_env=False)
    if response.status_code != 200 or not any(item.get("id") == model for item in response.json().get("data", [])):
        return False
    response = httpx.post(url + "/v1/chat/completions", headers=headers, timeout=30, trust_env=False,
                          json={"model": model, "messages": [{"role": "user", "content": "Hi /no_think"}],
                                "max_tokens": 1, "temperature": 0, "stream": False})
    return response.status_code == 200 and bool(response.json().get("choices"))
