"""Serve the community inference pool while this GPU is idle (API side: lumaris_api/inference_pool.py).

Every heartbeat the server may grant a short lease (`inference_worker` in the reply) naming one pinned
model. Under it we download the GGUF once (sha256-verified, resumable), run the pinned llama.cpp
server on 127.0.0.1 with a fresh random API key, open our reverse tunnel to the gateway, and report
`warm` in the next heartbeat. A claimed job, a live rental, a lapsed lease or the owner's veto
(PETABYTE_INFERENCE_WORKER=false) stops it: a rental always gets the GPU first.

An image lease ("kind": "image", Petabyte Create) runs stable-diffusion.cpp's sd-server the same way,
with its three pinned files (diffusion model, text encoder, VAE). sd-server has no API key, so it is
reached only through a localhost proxy that checks the session key and passes the generation paths.
A video lease ("mode": "vid_gen") is an image lease whose text encoder is UMT5 ("t5xxl", Wan 2.2), or
Qwen2.5-VL ("llm") plus ByT5 ("t5xxl") for HunyuanVideo 1.5.

Nothing the server sends can make this run something else: the image must be the official llama.cpp
(or stable-diffusion.cpp) repository pinned by digest, the weights must come from huggingface.co and
match the pinned hash.
"""
import hashlib
import hmac
import http.server
import json
import logging
import os
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import httpx

NAME = "petabyte-inference"
LABEL = "market.petabyte.inference=1"
MODELS = Path(os.getenv("PETABYTE_INFERENCE_MODELS", "/var/lib/petabyte-agent/inference"))
RETRY_AFTER_ERROR_S = 120
# The lease is <= 30 s and renewed every heartbeat (15 s), so ONE late or discarded heartbeat put the
# next renewal just past expiry and tore down a warm server (2060, 2026-10-06). A rental still stops
# it at once (before_work); this only stops a single slow heartbeat from forcing a full reload.
LEASE_GRACE_S = 20
READY_TIMEOUT_S = 300
_IMAGE_RE = re.compile(r"^(ghcr\.io/ggml-org/llama\.cpp|docker\.io/petabytecloud/pool-llama)@sha256:[0-9a-f]{64}$")
_URL_RE = re.compile(r"^https://huggingface\.co/[A-Za-z0-9._/-]+\.gguf$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_MODEL_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
# huggingface.co is unreachable from mainland China (spec 270 stepped down from both big models in
# minutes, 2026-10-09), and so was hf-mirror.com from that node (2026-10-10). registry.petabyte.market/hf
# serves the catalog's files from huggingface.co (deploy/registry-mirror). Every file is sha256-pinned
# and checked over the whole download, so a source can deliver a file, resume another's partial, but
# never alter it.
HF_SOURCES = ("https://huggingface.co", "https://registry.petabyte.market/hf", "https://hf-mirror.com")
# Image leases: stable-diffusion.cpp's official CUDA image, weights as .gguf or .safetensors.
_SD_IMAGE_RE = re.compile(r"^(ghcr\.io/leejet/stable-diffusion\.cpp|docker\.io/petabytecloud/pool-sd)@sha256:[0-9a-f]{64}$")
_FILE_URL_RE = re.compile(r"^https://huggingface\.co/[A-Za-z0-9._/-]+\.(gguf|safetensors)$")
_ROLES = ("diffusion", "llm", "vae")
_VIDEO_ROLES = (("diffusion", "t5xxl", "vae"),        # Wan: UMT5 text encoder (sd-server --t5xxl)
                ("diffusion", "llm", "t5xxl", "vae")) # HunyuanVideo 1.5: Qwen2.5-VL --llm + ByT5 --t5xxl
_CHECKPOINT_ROLES = ("checkpoint",)                 # complete SDXL: embedded CLIP encoders and VAE
# sd-server flags a lease may set: tuning only, never a path, a port or something to serve.
_SD_FLAGS = {"--cfg-scale", "--sampling-method", "--scheduler", "--flow-shift", "--steps", "--clip-skip"}
_SD_SWITCHES = {"--diffusion-fa", "--offload-to-cpu", "--vae-tiling"}
_SD_VALUE_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
# The only server paths our API calls; the proxy refuses the rest (upscale, web UIs, model loading).
_IMAGE_PATHS = re.compile(r"^/(v1/images/generations|v1/audio/speech|inference|pb/img/(rembg|upscale)|"
                          r"sdcpp/v1/(capabilities|img_gen|vid_gen|jobs/[A-Za-z0-9_-]{1,64}(/cancel)?))$")
# Image tools (Petabyte Create, Phase 1): Petabyte's CPU server, its models baked into the pinned image.
_IMGTOOLS_IMAGE_RE = re.compile(r"^docker\.io/petabytecloud/pool-imgtools@sha256:[0-9a-f]{64}$")
# Audio leases: whisper.cpp's official CUDA image (speech to text) and Petabyte's Chatterbox image
# (deploy/tts-server, text to speech). Each file is mounted at /models/<role>: role = the file name the
# server loads, so it may not name a path.
_AUDIO_IMAGE_RE = {"stt": re.compile(r"^docker\.io/petabytecloud/pool-whisper@sha256:[0-9a-f]{64}$"),
                   "tts": re.compile(r"^docker\.io/petabytecloud/tts-chatterbox@sha256:[0-9a-f]{64}$")}
_AUDIO_URL_RE = re.compile(r"^https://huggingface\.co/[A-Za-z0-9._/-]+\.(bin|pt|json|safetensors)$")
_AUDIO_ROLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# Long speech to text (owner, 2026-10-18): the API gives the node a short-lived bucket URL for a file of
# up to STT_MAX_BYTES and polls the job. whisper-server has no job API, so AuthProxy runs the job.
_STT_JOB_PATH = re.compile(r"^/pb/stt/jobs(?:/([0-9a-f]{16}))?$")
_STT_URL_RE = re.compile(r"^https://[A-Za-z0-9.-]+\.(digitaloceanspaces\.com|aliyuncs\.com)/\S+$")
# Burned-in subtitles: the finished video goes to the bucket with the API's signed, size-capped POST.
_BURN_URL_RE = re.compile(r"^https://[A-Za-z0-9.-]+\.(digitaloceanspaces\.com|aliyuncs\.com)(/\S*)?$")
BURN_STYLE = "FontName=DejaVu Sans,FontSize=20,Outline=2"    # DejaVu covers Arabic (pool-whisper's fonts)
STT_MAX_BYTES = 512 * 1024 * 1024
STT_TIMEOUT_S = 6 * 3600
STT_KEEP_S = 3600                 # a finished job's result waits this long for the API to collect it
IMAGE_BENCH = {"prompt": "a lighthouse on a cliff at sunset, photograph", "size": "512x512"}
# A video model cannot make a still: its benchmark is a tiny clip (9 frames at 256x256, 4 steps).
VIDEO_BENCH = {"prompt": "waves rolling onto a beach at sunset", "width": 256, "height": 256,
               "video_frames": 9, "sample_params": {"sample_steps": 4}}


class Cancelled(Exception):
    pass


def docker(*args, timeout=30):
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, check=False)


def running(name=NAME):
    r = docker("ps", "-q", "--filter", f"name=^/{name}$", "--filter", f"label={LABEL}")
    return r.returncode == 0 and bool(r.stdout.strip())


def stop_container(name=NAME):
    """Remove only our labelled server container (also an orphan from a previous agent run)."""
    r = docker("ps", "-aq", "--filter", f"name=^/{name}$", "--filter", f"label={LABEL}")
    for cid in r.stdout.split():
        if re.fullmatch(r"[0-9a-f]{12,64}", cid):
            docker("rm", "-f", cid)


def stop_all_containers():
    """Agent restart: reclaim only managed server names carrying our ownership label."""
    r = docker("ps", "-a", "--filter", f"label={LABEL}", "--format", "{{.ID}} {{.Names}}")
    for line in r.stdout.splitlines():
        fields = line.split()
        if (len(fields) == 2 and re.fullmatch(r"[0-9a-f]{12,64}", fields[0])
                and re.fullmatch(r"petabyte-inference(?:-inf-[0-9a-f]{16})?", fields[1])):
            docker("rm", "-f", fields[0])


# llama-server flags a chat lease may set: tuning only (Qwen3.8's --reasoning-budget 0 turns thinking off).
_LLM_FLAGS = {"--reasoning-budget"}


def _valid_args(args, flags=_SD_FLAGS, switches=_SD_SWITCHES):
    if not isinstance(args, list) or len(args) > 16:
        return False
    i = 0
    while i < len(args):
        if args[i] in switches:
            i += 1
        elif (args[i] in flags and i + 1 < len(args) and isinstance(args[i + 1], str)
              and _SD_VALUE_RE.match(args[i + 1])):
            i += 2
        else:
            return False
    return True


def _valid_image(p):
    files = p.get("files")
    checkpoint = p.get("architecture") == "sdxl"
    n = len(files) if isinstance(files, list) else 0
    roles = (_CHECKPOINT_ROLES if checkpoint else _ROLES if p.get("mode") != "vid_gen" else
             next((r for r in _VIDEO_ROLES if len(r) == n), _VIDEO_ROLES[0]))
    return (isinstance(p.get("model"), str) and _MODEL_RE.match(p["model"])
            and p.get("mode", "img_gen") in ("img_gen", "vid_gen")
            and p.get("architecture") in (None, "sdxl")
            and (not checkpoint or p.get("mode", "img_gen") == "img_gen")
            and isinstance(p.get("image"), str) and _SD_IMAGE_RE.match(p["image"])
            and isinstance(files, list) and len(files) == len(roles)
            and all(isinstance(f, dict) for f in files)
            and sorted(f.get("role") or "" for f in files) == sorted(roles)
            and all(isinstance(f.get("url"), str) and _FILE_URL_RE.match(f["url"])
                    and isinstance(f.get("sha256"), str) and _SHA_RE.match(f["sha256"])
                    and type(f.get("size")) is int and 1 <= f["size"] <= 100 * 1024 ** 3 for f in files)
            and _valid_args(p.get("args", [])) and type(p.get("seconds")) is int and 1 <= p["seconds"] <= 30)


def _valid_audio(p):
    files, mode = p.get("files"), p.get("mode")
    return (isinstance(p.get("model"), str) and _MODEL_RE.match(p["model"]) and mode in _AUDIO_IMAGE_RE
            and isinstance(p.get("image"), str) and _AUDIO_IMAGE_RE[mode].match(p["image"])
            and isinstance(files, list) and 1 <= len(files) <= 12 and all(isinstance(f, dict) for f in files)
            and len({f.get("role") for f in files}) == len(files)
            and all(isinstance(f.get("role"), str) and _AUDIO_ROLE_RE.match(f["role"])
                    and isinstance(f.get("url"), str) and _AUDIO_URL_RE.match(f["url"])
                    and isinstance(f.get("sha256"), str) and _SHA_RE.match(f["sha256"])
                    and type(f.get("size")) is int and 1 <= f["size"] <= 20 * 1024 ** 3 for f in files)
            and p.get("args", []) == [] and type(p.get("seconds")) is int and 1 <= p["seconds"] <= 30)


def _valid_vision(p):
    """llama.cpp's server (the chat pool's pinned image) with a model and its vision projector."""
    files = p.get("files")
    return (isinstance(p.get("model"), str) and _MODEL_RE.match(p["model"])
            and isinstance(p.get("image"), str) and _IMAGE_RE.match(p["image"])
            and isinstance(files, list) and len(files) == 2 and all(isinstance(f, dict) for f in files)
            and sorted(f.get("role") or "" for f in files) == ["mmproj", "model"]
            and all(isinstance(f.get("url"), str) and _URL_RE.match(f["url"])
                    and isinstance(f.get("sha256"), str) and _SHA_RE.match(f["sha256"])
                    and type(f.get("size")) is int and 1 <= f["size"] <= 100 * 1024 ** 3 for f in files)
            and type(p.get("ctx")) is int and 2048 <= p["ctx"] <= 131072
            and type(p.get("parallel")) is int and 1 <= p["parallel"] <= 8
            and p.get("args", []) == [] and type(p.get("seconds")) is int and 1 <= p["seconds"] <= 30)


def _valid_imgtools(p):
    return (isinstance(p.get("model"), str) and _MODEL_RE.match(p["model"])
            and isinstance(p.get("image"), str) and _IMGTOOLS_IMAGE_RE.match(p["image"])
            and p.get("files") == [] and type(p.get("seconds")) is int and 1 <= p["seconds"] <= 30)


def valid(p):
    """The lease's shape, and that it names only the allowed image and weights."""
    def num(v, lo, hi):
        return type(v) is int and lo <= v <= hi
    if isinstance(p, dict) and p.get("kind") == "image":
        return bool(_valid_image(p))
    if isinstance(p, dict) and p.get("kind") == "audio":
        return bool(_valid_audio(p))
    if isinstance(p, dict) and p.get("kind") == "vision":
        return bool(_valid_vision(p))
    if isinstance(p, dict) and p.get("kind") == "imgtools":
        return bool(_valid_imgtools(p))
    if isinstance(p, dict) and p.get("kind") is not None:
        return False
    return (isinstance(p, dict) and isinstance(p.get("model"), str) and _MODEL_RE.match(p["model"])
            and isinstance(p.get("url"), str) and _URL_RE.match(p["url"])
            and isinstance(p.get("sha256"), str) and _SHA_RE.match(p["sha256"])
            and isinstance(p.get("image"), str) and _IMAGE_RE.match(p["image"])
            and num(p.get("size"), 1, 200 * 1024 ** 3) and num(p.get("ctx"), 512, 131072)
            and num(p.get("parallel"), 1, 8) and num(p.get("seconds"), 1, 30)
            and isinstance(p.get("offload", False), bool)
            and _valid_args(p.get("args", []), _LLM_FLAGS, set()))


# A big (MoE) model split between GPU and RAM: --fit keeps what fits in VRAM (256 MiB margin) and the
# rest of the experts in RAM; --no-mmap loads them into memory instead of paging them from disk.
# Tuned on an RTX 4080 + 31 GiB with Qwen3-Next-80B: 21 tok/s vs 7 tok/s mmap'd (2026-10-09).
OFFLOAD_ARGS = ("--fit", "on", "--fit-target", "256", "--no-mmap")


def _target(p, root=None):
    return (root or MODELS) / (p["sha256"] + (".safetensors" if p["url"].endswith(".safetensors") else ".gguf"))


_FETCH_LOCKS, _FETCH_LOCK = {}, threading.Lock()


def fetch_model(p, cancelled=lambda: False, keep=(), root=None, evict=True):
    # Shared model servers can have the same encoder/VAE. Only one writer may own a .part file.
    identity = str(_target(p, root).resolve())
    with _FETCH_LOCK:
        lock = _FETCH_LOCKS.setdefault(identity, threading.Lock())
    while not lock.acquire(timeout=1):
        if cancelled():
            raise Cancelled()
    try:
        return _fetch_model(p, cancelled, keep, root, evict)
    finally:
        lock.release()


def _fetch_model(p, cancelled=lambda: False, keep=(), root=None, evict=True):
    """The pinned file on local disk, verified. Resumes a partial download. Keeps only this lease's
    files: `keep` names the lease's other files (an image model is three). A batch job passes its own
    `root` with evict=False, so it never deletes the pool's cached models."""
    folder = root or MODELS
    folder.mkdir(parents=True, exist_ok=True)
    path, part = _target(p, root), folder / f"{p['sha256']}.part"
    if path.exists() and path.stat().st_size == p["size"]:
        return path
    have = part.stat().st_size if part.exists() else 0
    if shutil.disk_usage(folder).free < p["size"] - have + 2 * 1024 ** 3:
        raise RuntimeError("not enough free disk for the inference model")
    h = hashlib.sha256()
    if have:
        with open(part, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                if cancelled():
                    raise Cancelled()
                h.update(chunk)
    errors = []
    for base in HF_SOURCES:
        url = base + p["url"][len(HF_SOURCES[0]):]
        headers = {"User-Agent": "petabyte-agent", **({"Range": f"bytes={have}-"} if have else {})}
        try:
            with httpx.stream("GET", url, headers=headers, follow_redirects=True, timeout=60,
                              trust_env=False) as r:
                r.raise_for_status()
                if have and r.status_code != 206:         # server ignored the range: start over
                    have, h = 0, hashlib.sha256()
                with open(part, "ab" if have else "wb") as f:
                    for chunk in r.iter_bytes(1 << 20):
                        if cancelled():
                            raise Cancelled()
                        have += len(chunk)
                        if have > p["size"]:
                            break
                        h.update(chunk)
                        f.write(chunk)
            break
        except httpx.HTTPError as e:                      # unreachable, or dropped mid-file: next source
            errors.append(f"{base.split('//')[1]}: {type(e).__name__}")
    else:
        raise RuntimeError("model download failed (" + "; ".join(errors) + ")")
    if have != p["size"] or h.hexdigest() != p["sha256"]:
        part.unlink(missing_ok=True)
        raise RuntimeError("inference model failed its hash check; discarded")
    os.replace(part, path)
    if not evict:
        return path
    keep = {path, *keep}
    for old in MODELS.iterdir():
        if old not in keep and not (old.suffix == ".part" and any(k.stem == old.stem for k in keep)):
            old.unlink(missing_ok=True)
    return path


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def bench(url, key):
    """Decode tokens/s on one fixed prompt. Only seeds the tier: the API measures live traffic."""
    t = time.monotonic()
    r = httpx.post(f"{url}/v1/chat/completions", headers={"Authorization": f"Bearer {key}"},
                   json={"messages": [{"role": "user", "content": "Count from 1 to 60, separated by spaces."}],
                         "max_tokens": 128, "temperature": 0}, timeout=180, trust_env=False)
    r.raise_for_status()
    j = r.json()
    tps = (j.get("timings") or {}).get("predicted_per_second")
    if not tps:
        tps = (j.get("usage") or {}).get("completion_tokens", 0) / max(time.monotonic() - t, 1e-3)
    return round(float(tps), 1)


def bench_image(url):
    """Megapixels per second for one fixed 512x512 image (seeds the tier; the API measures live jobs)."""
    t = time.monotonic()
    r = httpx.post(f"{url}/v1/images/generations", json=IMAGE_BENCH, timeout=600, trust_env=False)
    r.raise_for_status()
    if not (r.json().get("data") or [{}])[0].get("b64_json"):
        raise RuntimeError("the image server returned no image")
    return round(512 * 512 / 1e6 / max(time.monotonic() - t, 1e-3), 4)


def bench_video(url, limit_s=900):
    """Frame-megapixels per second for one tiny clip (seeds the tier; the API measures live jobs)."""
    t = time.monotonic()
    r = httpx.post(f"{url}/sdcpp/v1/vid_gen", json=VIDEO_BENCH, timeout=60, trust_env=False)
    r.raise_for_status()
    job = r.json().get("id")
    if not isinstance(job, str):
        raise RuntimeError("the video server did not accept the benchmark clip")
    while time.monotonic() - t < limit_s:
        time.sleep(3)
        j = httpx.get(f"{url}/sdcpp/v1/jobs/{job}", timeout=60, trust_env=False).json()
        if j.get("status") == "completed":
            if not (j.get("result") or {}).get("b64_json"):
                raise RuntimeError("the video server returned no clip")
            px = VIDEO_BENCH["width"] * VIDEO_BENCH["height"] * VIDEO_BENCH["video_frames"]
            return round(px / 1e6 / max(time.monotonic() - t, 1e-3), 4)
        if j.get("status") in ("failed", "cancelled"):
            raise RuntimeError(f"the benchmark clip {j['status']}: {str(j.get('error'))[:120]}")
    raise RuntimeError("the benchmark clip did not finish")


def _tone_wav(seconds=4, rate=16000):
    """A short 440 Hz tone as a WAV file: the speech-to-text benchmark input."""
    import io
    import math
    import struct
    import wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate)))
                               for i in range(seconds * rate)))
    return buf.getvalue()


def bench_stt(url):
    """Seconds of audio transcribed per second (x real time), on a 4 s tone."""
    t = time.monotonic()
    r = httpx.post(f"{url}/inference", files={"file": ("bench.wav", _tone_wav())},
                   data={"response_format": "verbose_json", "language": "en"}, timeout=300, trust_env=False)
    r.raise_for_status()
    return round(4 / max(time.monotonic() - t, 1e-3), 2)


def bench_tts(url):
    """Seconds of speech made per second (x real time), on one short English sentence."""
    t = time.monotonic()
    r = httpx.post(f"{url}/v1/audio/speech", json={"input": "Hello from Petabyte.", "language": "en"},
                   timeout=600, trust_env=False)
    r.raise_for_status()
    if r.content[:4] != b"RIFF":
        raise RuntimeError("the speech server returned no audio")
    return round(max(len(r.content) - 44, 0) / (24000 * 2) / max(time.monotonic() - t, 1e-3), 2)


def _tiny_png(side=64):
    """A plain grey PNG: the image tools' benchmark input (the server resizes it to its working size)."""
    import struct
    import zlib
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))
    rows = b"".join(b"\x00" + b"\x80" * (side * 3) for _ in range(side))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", side, side, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def bench_imgtools(url):
    """Images per second for one background removal (the heavier tool, at its full working size)."""
    t = time.monotonic()
    r = httpx.post(f"{url}/pb/img/rembg", content=_tiny_png(), timeout=300, trust_env=False)
    r.raise_for_status()
    if r.content[:4] != b"\x89PNG":
        raise RuntimeError("the image tools server returned no image")
    return round(1 / max(time.monotonic() - t, 1e-3), 3)


def srt(result):
    """whisper's verbose_json segments as SRT (lumaris_api/audio_pool.render, for burning in)."""
    def stamp(x):
        ms = int(round(max(float(x), 0) * 1000))
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"
    segs = [s for s in result.get("segments") or [] if isinstance(s, dict) and str(s.get("text", "")).strip()]
    return "\n".join(f"{i}\n{stamp(s['start'])} --> {stamp(s['end'])}\n{str(s['text']).strip()}\n"
                     for i, s in enumerate(segs, 1))


def _retire(image):
    try:
        import template_storage
        n = template_storage.retire_superseded(image)
        if n:
            logging.info("inference worker: deleted %d image(s) %s replaces", n, image.split("@")[0])
    except Exception as e:                                # noqa: BLE001 — cleanup never stops serving
        logging.warning("inference worker: image cleanup failed: %s", e)


class SttJobs:
    """Speech-to-text jobs: download the buyer's upload from a short-lived bucket URL into scratch (RAM
    when the node has it), transcribe it on whisper-server, and keep the result until the API polls it.
    One transcription runs at a time; the rest queue. A job with `burn` (and an `image`: pool-whisper,
    whose ffmpeg has libass and Arabic-capable fonts) then burns the subtitles into the video in a
    one-shot, network-less container and uploads it with the API's signed POST."""

    def __init__(self, inner_port, image=None):
        self.inner, self.jobs, self.image = inner_port, {}, image
        self.lock, self.gpu = threading.Lock(), threading.Lock()
        self.burning = set()

    def close(self):
        """The server is stopping (a rental, a lapsed lease): no burn may outlive it on the CPU."""
        for name in list(self.burning):
            docker("rm", "-f", name)

    def start(self, body):
        """(HTTP status, reply) for POST /pb/stt/jobs {url, filename, language, burn: {url, fields}}."""
        url, language = str(body.get("url") or ""), str(body.get("language") or "auto")
        name = re.sub(r"[^A-Za-z0-9._ -]", "_", str(body.get("filename") or "audio"))[:120] or "audio"
        if not _STT_URL_RE.match(url) or language not in ("auto", "ar", "en"):
            return 400, {"error": "bad job"}
        burn = body.get("burn")
        if burn is not None and not (
                self.image and isinstance(burn, dict) and isinstance(burn.get("url"), str)
                and _BURN_URL_RE.match(burn["url"]) and isinstance(burn.get("fields"), dict)
                and len(burn["fields"]) <= 16
                and all(isinstance(k, str) and isinstance(v, str) for k, v in burn["fields"].items())):
            return 400, {"error": "bad job"}
        with self.lock:
            now = time.time()
            for jid in [j for j, v in self.jobs.items() if v.get("completed") and now - v["completed"] > STT_KEEP_S]:
                del self.jobs[jid]
            if sum(v["status"] in ("queued", "running") for v in self.jobs.values()) >= 4:
                return 429, {"error": "too many jobs"}
            jid = secrets.token_hex(8)
            self.jobs[jid] = {"status": "queued"}
        threading.Thread(target=self._work, args=(jid, url, name, language, burn), daemon=True,
                         name=f"pb-stt-{jid}").start()
        return 202, {"id": jid, "status": "queued"}

    def get(self, jid):
        with self.lock:
            job = self.jobs.get(jid)
            return (200, dict(job)) if job else (404, {"error": "job not found"})

    def _burn(self, jid, work, result, burn):
        """ffmpeg (libx264, at most 1920 wide) with the transcript as SRT, then the signed upload."""
        with open(os.path.join(work, "subs.srt"), "w", encoding="utf-8") as f:
            f.write(srt(result))
        root = os.getuid() == 0
        if root:                                  # the container runs as nobody: let it read and write /w
            for p in (work, os.path.join(work, "upload"), os.path.join(work, "subs.srt")):
                os.chown(p, 65534, 65534)
        name, user = f"petabyte-burn-{jid}", "65534:65534" if root else f"{os.getuid()}:{os.getgid()}"
        self.burning.add(name)
        try:
            r = docker("run", "--rm", "--name", name, "--network=none", "--read-only", "--cap-drop=ALL",
                       "--security-opt=no-new-privileges", "--pids-limit=256", f"--user={user}",
                       "--memory=2g", "--cpus=2", "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
                       "--log-opt=max-size=1m", "-v", f"{work}:/w", "--entrypoint", "ffmpeg", self.image,
                       "-nostdin", "-hide_banner", "-loglevel", "error", "-i", "/w/upload",
                       "-vf", f"scale='trunc(min(1920,iw)/2)*2':-2,subtitles=/w/subs.srt:force_style='{BURN_STYLE}'",
                       "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-c:a", "aac", "-b:a", "128k",
                       "-movflags", "+faststart", "-y", "/w/out.mp4", timeout=STT_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            raise RuntimeError("burning the subtitles took too long")
        finally:
            docker("rm", "-f", name)
            self.burning.discard(name)
        out = os.path.join(work, "out.mp4")
        if r.returncode or not os.path.exists(out):
            raise RuntimeError("ffmpeg could not burn the subtitles: " + r.stderr.strip()[-160:])
        with open(out, "rb") as f:
            u = httpx.post(burn["url"], data=burn["fields"], files={"file": ("subtitled.mp4", f, "video/mp4")},
                           timeout=httpx.Timeout(60, write=900), trust_env=False)
        if u.status_code not in (200, 201, 204):
            raise RuntimeError(f"the subtitled video could not be uploaded ({u.status_code})")

    def _work(self, jid, url, name, language, burn=None):
        job, work = self.jobs[jid], None
        with self.gpu:
            job.update(status="running", started=int(time.time()))
            try:
                import agent_scratch
                # a burn keeps the upload and writes the subtitled video beside it
                work = agent_scratch.make_scratch("pb-stt-", need_bytes=STT_MAX_BYTES * (2 if burn else 1))
                path, n = os.path.join(work, "upload"), 0
                with httpx.stream("GET", url, timeout=httpx.Timeout(60, read=300), trust_env=False) as r:
                    if r.status_code != 200:
                        raise RuntimeError(f"the upload could not be downloaded ({r.status_code})")
                    with open(path, "wb") as f:
                        for chunk in r.iter_bytes(1 << 20):
                            n += len(chunk)
                            if n > STT_MAX_BYTES:
                                raise RuntimeError("the file is too large")
                            f.write(chunk)
                with open(path, "rb") as f:
                    r = httpx.post(f"http://127.0.0.1:{self.inner}/inference", files={"file": (name, f)},
                                   data={"language": language, "response_format": "verbose_json",
                                         "temperature": "0.0"}, timeout=STT_TIMEOUT_S, trust_env=False)
                if r.status_code != 200:
                    raise RuntimeError(f"the speech server refused the file ({r.status_code})")
                result = r.json()
                if burn:                          # the transcript is good either way; the API bills what worked
                    job.update(stage="burning")
                    try:
                        self._burn(jid, work, result, burn)
                        job["burned"] = True
                    except Exception as e:        # noqa: BLE001 — reported, the transcript still completes
                        job["burn_error"] = str(e)[:200]
                job.update(status="completed", result=result, completed=int(time.time()))
            except Exception as e:                        # noqa: BLE001 — reported to the API, never raised
                job.update(status="failed", error=str(e)[:200], completed=int(time.time()))
            finally:
                if work:
                    shutil.rmtree(work, ignore_errors=True)


class AuthProxy:
    """127.0.0.1:<port> -> sd-server. sd-server has no API key, so the gateway, and anyone who learns
    the worker's address, reaches it only through this: the session key, and only _IMAGE_PATHS.
    A speech-to-text proxy (stt_jobs) also runs the long-file jobs itself (SttJobs, /pb/stt/jobs)."""

    def __init__(self, inner_port, key, stt_jobs=False, image=None):
        jobs = self.jobs = SttJobs(inner_port, image) if stt_jobs else None
        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _reply(self, code, body, ctype="application/json"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _forward(self):
                auth = self.headers.get("Authorization") or ""
                job = _STT_JOB_PATH.match(self.path.split("?", 1)[0]) if jobs else None
                if not hmac.compare_digest(auth.encode(), f"Bearer {key}".encode()) or not (
                        job or _IMAGE_PATHS.match(self.path.split("?", 1)[0])):
                    return self._reply(403, b'{"error":"forbidden"}')
                if job:
                    return self._job(job.group(1))
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    n = -1
                if n < 0:                                 # read(-1) would block until the client hangs up
                    return self._reply(400, b'{"error":"bad Content-Length"}')
                if n > 32 * 1024 * 1024:
                    return self._reply(413, b'{"error":"request too large"}')
                data = self.rfile.read(n) if n else None
                try:
                    r = httpx.request(self.command, f"http://127.0.0.1:{inner_port}{self.path}", content=data,
                                      headers={"Content-Type": self.headers.get("Content-Type") or "application/json"},
                                      timeout=900, trust_env=False)
                except httpx.HTTPError:
                    return self._reply(502, b'{"error":"image server unavailable"}')
                self._reply(r.status_code, r.content, r.headers.get("content-type") or "application/json")

            def _job(self, jid):
                if self.command == "GET" and jid:
                    code, out = jobs.get(jid)
                elif self.command == "POST" and not jid:
                    try:
                        n = int(self.headers.get("Content-Length") or 0)
                        body = json.loads(self.rfile.read(n) if 0 < n <= 8192 else b"null")
                    except (ValueError, UnicodeDecodeError):
                        body = None
                    code, out = jobs.start(body) if isinstance(body, dict) else (400, {"error": "bad job"})
                else:
                    code, out = 405, {"error": "method not allowed"}
                self._reply(code, json.dumps(out).encode())

            do_GET = do_POST = _forward

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True, name="pb-image-proxy").start()

    def close(self):
        if self.jobs:
            self.jobs.close()
        self.server.shutdown()
        self.server.server_close()


def _lease_key(p):
    """What identifies a lease (everything but its renewal time)."""
    return json.dumps({k: v for k, v in p.items() if k != "seconds"}, sort_keys=True)


class Controller:
    def __init__(self, name=NAME, gpu_args=None, shared=False):
        self.name = name
        self.gpu_args = gpu_args or ["--gpus", "all"]
        self.shared = shared
        self.endpoint = None
        self._resources = []
        self.lock = threading.RLock()
        self.generation = 0
        self.busy = False
        self.gateway = "us"
        # task_fetcher injects its reverse-tunnel helpers (attach); inert until then.
        self.open_tunnel = lambda port: None
        self.close_tunnel = lambda: None
        self.tunnel_alive = lambda: False
        self._state = {"state": "off"}
        self._thread, self._stop, self._key = None, threading.Event(), None
        self._proxy = None
        self._until, self._retry_at, self._retry_key = 0.0, 0.0, None

    def attach(self, open_tunnel, close_tunnel, tunnel_alive):
        self.open_tunnel, self.close_tunnel, self.tunnel_alive = open_tunnel, close_tunnel, tunnel_alive

    def enabled(self):
        """On by default; PETABYTE_INFERENCE_WORKER=false vetoes it."""
        return os.getenv("PETABYTE_INFERENCE_WORKER", "true").strip().lower() != "false"

    def ticket(self):
        with self.lock:
            return self.generation

    def report(self):
        """The heartbeat's `inference` field (the key only while warm). None when vetoed."""
        return dict(self._state) if self.enabled() else None

    def _set(self, **state):
        self._state = state

    def _stop_container(self):
        stop_container() if self.name == NAME else stop_container(self.name)

    def _running(self):
        return running() if self.name == NAME else running(self.name)

    def _fetch(self, p, cancelled, keep=()):
        if self.shared:
            return fetch_model(p, cancelled, keep, evict=False)
        return fetch_model(p, cancelled, keep)

    def before_work(self):
        """A job was claimed: free the GPU NOW, before the job touches it."""
        with self.lock:
            self.generation += 1
            self.busy = True
            self.stop()

    def after_work(self):
        """job_loop calls this after EVERY poll, job or not. Only a poll that ran a job (before_work)
        moved the generation; bumping it on idle polls discarded heartbeats in flight every ~5 s."""
        with self.lock:
            self.busy = False

    def stop(self):
        t = self._thread
        self._stop.set()
        if t and t is not threading.current_thread():
            t.join(timeout=30)
        self._thread, self._key = None, None
        try:
            self.close_tunnel()
            self._stop_container()
            self.endpoint = None
            if self._proxy:
                self._proxy.close()
                self._proxy = None
        except Exception as e:                            # noqa: BLE001 — a rental still proceeds
            logging.warning("inference worker stop: %s", e)
        if self._state.get("state") != "error":
            self._set(state="off")

    def heartbeat(self, ticket, permit, live=False):
        with self.lock:
            if ticket != self.generation:
                return                                    # a job started since this heartbeat left
            if not permit or self.busy or live or not self.enabled() or not valid(permit):
                if self._thread or self._state.get("state") not in ("off", "error"):
                    self.stop()
                return
            self._until = time.monotonic() + permit["seconds"] + LEASE_GRACE_S
            key = _lease_key(permit)
            if self._thread and self._thread.is_alive() and self._key == key:
                return                                    # lease renewed; already serving it
            if time.monotonic() < self._retry_at and key == self._retry_key:
                return                                    # cool down a failed lease, not its replacement
            self.stop()
            self._stop = threading.Event()
            self._key = key
            self._thread = threading.Thread(target=self._run, args=(dict(permit), self._stop),
                                            daemon=True, name="pb-inference")
            self._thread.start()

    def _lapsed(self, stop):
        return stop.is_set() or time.monotonic() > self._until

    def _run(self, p, stop):
        failed = False
        try:
            self._set(state="downloading", model=p["model"])
            if p.get("kind") == "image":
                return self._serve_image(p, stop)
            if p.get("kind") == "audio":
                return self._serve_audio(p, stop)
            if p.get("kind") == "vision":
                return self._serve_vision(p, stop)
            if p.get("kind") == "imgtools":
                return self._serve_imgtools(p, stop)
            self._resources = (["--memory", f"{p['reserved_ram_mib']}m", "--cpus", "2"]
                               if self.shared else [])
            path = self._fetch(p, lambda: self._lapsed(stop))
            import template_storage                 # cache-managed, with the registry-mirror fallback
            template_storage.prepare(p["image"], 0, timeout=1800)
            image = template_storage.local(p["image"])
            if self._lapsed(stop):
                return
            self._set(state="starting", model=p["model"])
            self._stop_container()
            port, key = _free_port(), secrets.token_urlsafe(32)
            r = docker("run", "-d", "--rm", "--pull=never", "--name", self.name, "--label", LABEL,
                       "--restart=no", *self.gpu_args, *self._resources, "-p", f"127.0.0.1:{port}:8080",
                       "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                       "--pids-limit=256", "--user=65534:65534", "-e", "HOME=/tmp",
                       "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
                       "--log-opt=max-size=1m", "--log-opt=max-file=1",
                       "--mount", f"type=bind,src={path},dst=/models/model.gguf,readonly",
                       "-e", f"LLAMA_ARG_API_KEY={key}",
                       image, "-m", "/models/model.gguf", "--host", "0.0.0.0", "--port", "8080",
                       "-c", str(p["ctx"] * p["parallel"]), "-np", str(p["parallel"]),
                       *(OFFLOAD_ARGS if p.get("offload") else ("-ngl", "999")), *p.get("args", []),
                       timeout=60)
            if r.returncode:
                raise RuntimeError(f"inference server did not start: {r.stderr.strip()[:160]}")
            url, deadline = f"http://127.0.0.1:{port}", time.monotonic() + READY_TIMEOUT_S
            while True:
                if stop.wait(2) or self._lapsed(stop):
                    return
                try:
                    if httpx.get(f"{url}/health", timeout=3, trust_env=False).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if not self._running():
                    raise RuntimeError("inference server exited while loading the model")
                if time.monotonic() > deadline:
                    raise RuntimeError("inference server did not become ready")
            tps = bench(url, key)
            self._warm_until_done(p, stop, port, key, dict(ctx=p["ctx"], parallel=p["parallel"], bench_tps=tps))
        except Cancelled:
            pass
        except Exception as e:                            # noqa: BLE001 — report it, retry later
            failed = True
            logging.warning("inference worker: %s", e)
            self._set(state="error", model=p["model"], error=str(e)[:200])
            self._retry_at = time.monotonic() + RETRY_AFTER_ERROR_S
            self._retry_key = _lease_key(p)
        finally:
            if self.shared:
                # Shared controllers have generation-specific tunnel names and their own proxy.
                # A late benchmark/SSH attempt must still reclaim those after stop() timed out.
                try:
                    self.close_tunnel()
                    if self._proxy:
                        self._proxy.close()
                        self._proxy = None
                except Exception:  # noqa: BLE001 — the node's next cleanup can retry
                    pass
            # stop() already cleaned up (and a newer worker may own the container by now); only a
            # worker that ended on its own — lease lapsed or failed — tidies up after itself.
            if not stop.is_set():
                try:
                    self.close_tunnel()
                    self._stop_container()
                    self.endpoint = None
                    if self._proxy:
                        self._proxy.close()
                        self._proxy = None
                except Exception:                         # noqa: BLE001
                    pass
                if not failed:
                    self._set(state="off")

    def _serve_image(self, p, stop):
        """sd-server with the lease's three files, behind AuthProxy; raises like the LLM path."""
        targets = [_target(f) for f in p["files"]]
        self._resources = (["--memory", f"{p['reserved_ram_mib']}m", "--cpus", "2"]
                           if self.shared else [])
        paths = {f["role"]: self._fetch(f, lambda: self._lapsed(stop), keep=targets)
                 for f in sorted(p["files"], key=lambda f: f["size"])}
        import template_storage
        template_storage.prepare(p["image"], 0, timeout=1800)
        image = template_storage.local(p["image"])
        if self._lapsed(stop):
            return
        self._set(state="starting", model=p["model"])
        self._stop_container()
        inner, key = _free_port(), secrets.token_urlsafe(32)
        models = {role: f"/models/{role}{path.suffix}" for role, path in paths.items()}
        model_args = (("--model", models["checkpoint"]) if p.get("architecture") == "sdxl" else
                      ("--diffusion-model", models["diffusion"], "--vae", models["vae"],
                       *(a for role in ("llm", "t5xxl") if role in models for a in (f"--{role}", models[role]))))
        r = docker("run", "-d", "--rm", "--pull=never", "--name", self.name, "--label", LABEL,
                   "--restart=no", *self.gpu_args, *self._resources, "-p", f"127.0.0.1:{inner}:8080",
                   "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                   "--pids-limit=256", "--user=65534:65534", "-e", "HOME=/tmp",
                   "--tmpfs", "/tmp:rw,noexec,nosuid,size=256m",
                   "--log-opt=max-size=1m", "--log-opt=max-file=1",
                   *[a for role, path in paths.items()
                     for a in ("--mount", f"type=bind,src={path},dst={models[role]},readonly")],
                   "--entrypoint", "/sd-server", image,
                   "--listen-ip", "0.0.0.0", "--listen-port", "8080",
                   *model_args,
                   *p.get("args", []), timeout=60)
        if r.returncode:
            raise RuntimeError(f"image server did not start: {r.stderr.strip()[:160]}")
        url, deadline = f"http://127.0.0.1:{inner}", time.monotonic() + READY_TIMEOUT_S
        while True:
            if stop.wait(2) or self._lapsed(stop):
                return
            try:
                if httpx.get(f"{url}/sdcpp/v1/capabilities", timeout=3, trust_env=False).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if not self._running():
                raise RuntimeError("image server exited while loading the model")
            if time.monotonic() > deadline:
                raise RuntimeError("image server did not become ready")
        mps = bench_video(url) if p.get("mode") == "vid_gen" else bench_image(url)
        if self._lapsed(stop):
            return
        self._proxy = AuthProxy(inner, key)
        self._warm_until_done(p, stop, self._proxy.port, key, dict(kind="image", bench_mps=mps))

    def _serve_audio(self, p, stop):
        """whisper-server (stt) or Petabyte's Chatterbox server (tts) behind AuthProxy, each lease file
        mounted read-only at /models/<role>; raises like the other paths."""
        targets = [_target(f) for f in p["files"]]
        self._resources = (["--memory", f"{p['reserved_ram_mib']}m", "--cpus", "2"]
                           if self.shared else [])
        paths = {f["role"]: self._fetch(f, lambda: self._lapsed(stop), keep=targets)
                 for f in sorted(p["files"], key=lambda f: f["size"])}
        import template_storage
        template_storage.prepare(p["image"], 0, timeout=1800)
        image = template_storage.local(p["image"])
        if self._lapsed(stop):
            return
        self._set(state="starting", model=p["model"])
        self._stop_container()
        inner, key = _free_port(), secrets.token_urlsafe(32)
        stt = p["mode"] == "stt"
        cmd = (("--entrypoint", "/app/build/bin/whisper-server", image, "-m", "/models/model.bin",
                "--host", "0.0.0.0", "--port", "8080", "--convert", "--tmp-dir", "/tmp") if stt else (image,))
        r = docker("run", "-d", "--rm", "--pull=never", "--name", self.name, "--label", LABEL,
                   "--restart=no", *self.gpu_args, *self._resources, "-p", f"127.0.0.1:{inner}:8080",
                   "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                   "--pids-limit=256", "--user=65534:65534", "-e", "HOME=/tmp",
                   # the upload plus ffmpeg's WAV of it (32 KB/s): ~8 h of audio for a 500 MB file
                   "--tmpfs", f"/tmp:rw,noexec,nosuid,size={'2048m' if stt else '1024m'}",
                   "--log-opt=max-size=1m", "--log-opt=max-file=1",
                   *[a for role, path in paths.items()
                     for a in ("--mount", f"type=bind,src={path},dst=/models/{role},readonly")],
                   *cmd, timeout=60)
        if r.returncode:
            raise RuntimeError(f"audio server did not start: {r.stderr.strip()[:160]}")
        url, deadline = f"http://127.0.0.1:{inner}", time.monotonic() + READY_TIMEOUT_S
        while True:
            if stop.wait(2) or self._lapsed(stop):
                return
            try:
                if httpx.get(f"{url}/health", timeout=3, trust_env=False).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if not self._running():
                raise RuntimeError("audio server exited while loading the model")
            if time.monotonic() > deadline:
                raise RuntimeError("audio server did not become ready")
        speed = bench_stt(url) if stt else bench_tts(url)
        if self._lapsed(stop):
            return
        self._proxy = AuthProxy(inner, key, stt_jobs=stt, image=image if stt else None)
        self._warm_until_done(p, stop, self._proxy.port, key, dict(kind="audio", bench_mps=speed))

    def _ready(self, url, path, stop, what):
        """Wait for `url + path` to answer 200; False when the lease ended first."""
        deadline = time.monotonic() + READY_TIMEOUT_S
        while True:
            if stop.wait(2) or self._lapsed(stop):
                return False
            try:
                if httpx.get(url + path, timeout=3, trust_env=False).status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
            if not self._running():
                raise RuntimeError(f"{what} server exited while loading")
            if time.monotonic() > deadline:
                raise RuntimeError(f"{what} server did not become ready")

    def _serve_vision(self, p, stop):
        """llama-server with the model and its vision projector (--mmproj), its own API key like chat.
        --image-max-tokens keeps one page image (plus its transcript) inside the context."""
        targets = [_target(f) for f in p["files"]]
        self._resources = (["--memory", f"{p['reserved_ram_mib']}m", "--cpus", "2"]
                           if self.shared else [])
        paths = {f["role"]: self._fetch(f, lambda: self._lapsed(stop), keep=targets)
                 for f in sorted(p["files"], key=lambda f: f["size"])}
        import template_storage
        template_storage.prepare(p["image"], 0, timeout=1800)
        image = template_storage.local(p["image"])
        if self._lapsed(stop):
            return
        self._set(state="starting", model=p["model"])
        self._stop_container()
        port, key = _free_port(), secrets.token_urlsafe(32)
        r = docker("run", "-d", "--rm", "--pull=never", "--name", self.name, "--label", LABEL,
                   "--restart=no", *self.gpu_args, *self._resources, "-p", f"127.0.0.1:{port}:8080",
                   "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                   "--pids-limit=256", "--user=65534:65534", "-e", "HOME=/tmp",
                   "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
                   "--log-opt=max-size=1m", "--log-opt=max-file=1",
                   "--mount", f"type=bind,src={paths['model']},dst=/models/model.gguf,readonly",
                   "--mount", f"type=bind,src={paths['mmproj']},dst=/models/mmproj.gguf,readonly",
                   "-e", f"LLAMA_ARG_API_KEY={key}",
                   image, "-m", "/models/model.gguf", "--mmproj", "/models/mmproj.gguf",
                   "--image-max-tokens", str(p["ctx"] * 5 // 16), "--host", "0.0.0.0", "--port", "8080",
                   "-c", str(p["ctx"] * p["parallel"]), "-np", str(p["parallel"]), "-ngl", "999", timeout=60)
        if r.returncode:
            raise RuntimeError(f"vision server did not start: {r.stderr.strip()[:160]}")
        url = f"http://127.0.0.1:{port}"
        if not self._ready(url, "/health", stop, "vision"):
            return
        tps = bench(url, key)
        self._warm_until_done(p, stop, port, key, dict(kind="vision", bench_mps=tps))

    def _serve_imgtools(self, p, stop):
        """Petabyte's image tools server (deploy/pool-images/imgtools) behind AuthProxy. CPU only: no
        GPU is passed in, so it can never touch the chat model's VRAM."""
        import template_storage
        template_storage.prepare(p["image"], 0, timeout=1800)
        image = template_storage.local(p["image"])
        if self._lapsed(stop):
            return
        self._set(state="starting", model=p["model"])
        self._stop_container()
        inner, key = _free_port(), secrets.token_urlsafe(32)
        threads = max(1, min(4, (os.cpu_count() or 2) // 2))       # at most half the seller's CPU
        r = docker("run", "-d", "--rm", "--pull=never", "--name", self.name, "--label", LABEL,
                   "--restart=no", "--memory", f"{p.get('reserved_ram_mib', 4096)}m", "--cpus", str(threads),
                   "-e", f"PB_THREADS={threads}", "-p", f"127.0.0.1:{inner}:8080",
                   "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                   "--pids-limit=256", "--user=65534:65534", "-e", "HOME=/tmp",
                   "--tmpfs", "/tmp:rw,noexec,nosuid,size=256m",
                   "--log-opt=max-size=1m", "--log-opt=max-file=1", image, timeout=60)
        if r.returncode:
            raise RuntimeError(f"image tools server did not start: {r.stderr.strip()[:160]}")
        url = f"http://127.0.0.1:{inner}"
        if not self._ready(url, "/health", stop, "image tools"):
            return
        speed = bench_imgtools(url)
        if self._lapsed(stop):
            return
        self._proxy = AuthProxy(inner, key)
        self._warm_until_done(p, stop, self._proxy.port, key, dict(kind="imgtools", bench_mps=speed))

    def _warm_until_done(self, p, stop, port, key, extra):
        """Open the tunnel to `port`, report warm, and watch the server until the lease ends."""
        if self._lapsed(stop):
            return  # a slow benchmark must not publish a worker that was already stopped
        rp = self.open_tunnel(port)
        if not rp:
            raise RuntimeError("could not open the reverse tunnel to the gateway")
        warm = dict(state="warm", model=p["model"], tunnel_port=rp, gateway=self.gateway, key=key, **extra)
        self.endpoint = (f"http://127.0.0.1:{port}", key)
        self._set(**warm)
        logging.info("inference worker warm: %s (%s)", p["model"], extra)
        # It works: the official images (and older digests) a petabytecloud image replaces are dead weight.
        threading.Thread(target=_retire, args=(p["image"],), daemon=True, name="pb-retire-images").start()
        while not stop.wait(3):
                if self._lapsed(stop):
                    return                                # no renewal: the server stopped leasing
                if not self._running():
                    raise RuntimeError("inference server exited")
                if not self.tunnel_alive():
                    rp = self.open_tunnel(port)
                    if not rp:
                        raise RuntimeError("reverse tunnel to the gateway was lost")
                    self._set(**{**warm, "tunnel_port": rp})


from shared_inference import PoolController  # noqa: E402 — wraps the legacy Controller above

controller = PoolController()
