"""Serve the community inference pool while this GPU is idle (API side: lumaris_api/inference_pool.py).

Every heartbeat the server may grant a short lease (`inference_worker` in the reply) naming one pinned
model. Under it we download the GGUF once (sha256-verified, resumable), run the pinned llama.cpp
server on 127.0.0.1 with a fresh random API key, open our reverse tunnel to the gateway, and report
`warm` in the next heartbeat. A claimed job, a live rental, a lapsed lease or the owner's veto
(PETABYTE_INFERENCE_WORKER=false) stops it: a rental always gets the GPU first.

An image lease ("kind": "image", Petabyte Create) runs stable-diffusion.cpp's sd-server the same way,
with its three pinned files (diffusion model, text encoder, VAE). sd-server has no API key, so it is
reached only through a localhost proxy that checks the session key and passes the generation paths.

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
_IMAGE_RE = re.compile(r"^ghcr\.io/ggml-org/llama\.cpp@sha256:[0-9a-f]{64}$")
_URL_RE = re.compile(r"^https://huggingface\.co/[A-Za-z0-9._/-]+\.gguf$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_MODEL_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
# huggingface.co is unreachable from mainland China (spec 270 stepped down from both big models in
# minutes, 2026-10-09); hf-mirror.com mirrors it path for path. Every file is sha256-pinned and checked
# over the whole download, so a source can deliver a file, resume another's partial, but never alter it.
HF_SOURCES = ("https://huggingface.co", "https://hf-mirror.com")
# Image leases: stable-diffusion.cpp's official CUDA image, weights as .gguf or .safetensors.
_SD_IMAGE_RE = re.compile(r"^ghcr\.io/leejet/stable-diffusion\.cpp@sha256:[0-9a-f]{64}$")
_FILE_URL_RE = re.compile(r"^https://huggingface\.co/[A-Za-z0-9._/-]+\.(gguf|safetensors)$")
_ROLES = ("diffusion", "llm", "vae")
# sd-server flags a lease may set: tuning only, never a path, a port or something to serve.
_SD_FLAGS = {"--cfg-scale", "--sampling-method", "--scheduler", "--flow-shift", "--steps"}
_SD_SWITCHES = {"--diffusion-fa", "--offload-to-cpu", "--vae-tiling"}
_SD_VALUE_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
# The only sd-server paths our API calls; the proxy refuses the rest (video, upscale, the web UI).
_IMAGE_PATHS = re.compile(r"^/(v1/images/generations|sdcpp/v1/(capabilities|img_gen|"
                          r"jobs/[A-Za-z0-9_-]{1,64}(/cancel)?))$")
IMAGE_BENCH = {"prompt": "a lighthouse on a cliff at sunset, photograph", "size": "512x512"}


class Cancelled(Exception):
    pass


def docker(*args, timeout=30):
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, check=False)


def running():
    r = docker("ps", "-q", "--filter", f"name=^/{NAME}$", "--filter", f"label={LABEL}")
    return r.returncode == 0 and bool(r.stdout.strip())


def stop_container():
    """Remove only our labelled server container (also an orphan from a previous agent run)."""
    r = docker("ps", "-aq", "--filter", f"name=^/{NAME}$", "--filter", f"label={LABEL}")
    for cid in r.stdout.split():
        if re.fullmatch(r"[0-9a-f]{12,64}", cid):
            docker("rm", "-f", cid)


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
    return (isinstance(p.get("model"), str) and _MODEL_RE.match(p["model"])
            and isinstance(p.get("image"), str) and _SD_IMAGE_RE.match(p["image"])
            and isinstance(files, list) and len(files) == len(_ROLES)
            and all(isinstance(f, dict) for f in files)
            and sorted(f.get("role") or "" for f in files) == sorted(_ROLES)
            and all(isinstance(f.get("url"), str) and _FILE_URL_RE.match(f["url"])
                    and isinstance(f.get("sha256"), str) and _SHA_RE.match(f["sha256"])
                    and type(f.get("size")) is int and 1 <= f["size"] <= 100 * 1024 ** 3 for f in files)
            and _valid_args(p.get("args", [])) and type(p.get("seconds")) is int and 1 <= p["seconds"] <= 30)


def valid(p):
    """The lease's shape, and that it names only the allowed image and weights."""
    def num(v, lo, hi):
        return type(v) is int and lo <= v <= hi
    if isinstance(p, dict) and p.get("kind") == "image":
        return bool(_valid_image(p))
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


def _target(p):
    return MODELS / (p["sha256"] + (".safetensors" if p["url"].endswith(".safetensors") else ".gguf"))


def fetch_model(p, cancelled=lambda: False, keep=()):
    """The pinned file on local disk, verified. Resumes a partial download. Keeps only this lease's
    files: `keep` names the lease's other files (an image model is three)."""
    MODELS.mkdir(parents=True, exist_ok=True)
    path, part = _target(p), MODELS / f"{p['sha256']}.part"
    if path.exists() and path.stat().st_size == p["size"]:
        return path
    have = part.stat().st_size if part.exists() else 0
    if shutil.disk_usage(MODELS).free < p["size"] - have + 2 * 1024 ** 3:
        raise RuntimeError("not enough free disk for the inference model")
    h = hashlib.sha256()
    if have:
        with open(part, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
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


class AuthProxy:
    """127.0.0.1:<port> -> sd-server. sd-server has no API key, so the gateway, and anyone who learns
    the worker's address, reaches it only through this: the session key, and only _IMAGE_PATHS."""

    def __init__(self, inner_port, key):
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
                if (not hmac.compare_digest(auth.encode(), f"Bearer {key}".encode())
                        or not _IMAGE_PATHS.match(self.path.split("?", 1)[0])):
                    return self._reply(403, b'{"error":"forbidden"}')
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

            do_GET = do_POST = _forward

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True, name="pb-image-proxy").start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _lease_key(p):
    """What identifies a lease (everything but its renewal time)."""
    return json.dumps({k: v for k, v in p.items() if k != "seconds"}, sort_keys=True)


class Controller:
    def __init__(self):
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
            stop_container()
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
            path = fetch_model(p, lambda: self._lapsed(stop))
            import template_storage                 # cache-managed, with the registry-mirror fallback
            template_storage.prepare(p["image"], 0, timeout=1800)
            image = template_storage.local(p["image"])
            if self._lapsed(stop):
                return
            self._set(state="starting", model=p["model"])
            stop_container()
            port, key = _free_port(), secrets.token_urlsafe(32)
            r = docker("run", "-d", "--rm", "--pull=never", "--name", NAME, "--label", LABEL,
                       "--restart=no", "--gpus", "all", "-p", f"127.0.0.1:{port}:8080",
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
                if not running():
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
            # stop() already cleaned up (and a newer worker may own the container by now); only a
            # worker that ended on its own — lease lapsed or failed — tidies up after itself.
            if not stop.is_set():
                try:
                    self.close_tunnel()
                    stop_container()
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
        paths = {f["role"]: fetch_model(f, lambda: self._lapsed(stop), keep=targets)
                 for f in sorted(p["files"], key=lambda f: f["size"])}
        import template_storage
        template_storage.prepare(p["image"], 0, timeout=1800)
        image = template_storage.local(p["image"])
        if self._lapsed(stop):
            return
        self._set(state="starting", model=p["model"])
        stop_container()
        inner, key = _free_port(), secrets.token_urlsafe(32)
        models = {role: f"/models/{role}{path.suffix}" for role, path in paths.items()}
        r = docker("run", "-d", "--rm", "--pull=never", "--name", NAME, "--label", LABEL,
                   "--restart=no", "--gpus", "all", "-p", f"127.0.0.1:{inner}:8080",
                   "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                   "--pids-limit=256", "--user=65534:65534", "-e", "HOME=/tmp",
                   "--tmpfs", "/tmp:rw,noexec,nosuid,size=256m",
                   "--log-opt=max-size=1m", "--log-opt=max-file=1",
                   *[a for role, path in paths.items()
                     for a in ("--mount", f"type=bind,src={path},dst={models[role]},readonly")],
                   "--entrypoint", "/sd-server", image,
                   "--listen-ip", "0.0.0.0", "--listen-port", "8080",
                   "--diffusion-model", models["diffusion"], "--llm", models["llm"], "--vae", models["vae"],
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
            if not running():
                raise RuntimeError("image server exited while loading the model")
            if time.monotonic() > deadline:
                raise RuntimeError("image server did not become ready")
        mps = bench_image(url)
        self._proxy = AuthProxy(inner, key)
        self._warm_until_done(p, stop, self._proxy.port, key, dict(kind="image", bench_mps=mps))

    def _warm_until_done(self, p, stop, port, key, extra):
        """Open the tunnel to `port`, report warm, and watch the server until the lease ends."""
        rp = self.open_tunnel(port)
        if not rp:
            raise RuntimeError("could not open the reverse tunnel to the gateway")
        warm = dict(state="warm", model=p["model"], tunnel_port=rp, gateway=self.gateway, key=key, **extra)
        self._set(**warm)
        logging.info("inference worker warm: %s (%s)", p["model"], extra)
        while not stop.wait(3):
                if self._lapsed(stop):
                    return                                # no renewal: the server stopped leasing
                if not running():
                    raise RuntimeError("inference server exited")
                if not self.tunnel_alive():
                    rp = self.open_tunnel(port)
                    if not rp:
                        raise RuntimeError("reverse tunnel to the gateway was lost")
                    self._set(**{**warm, "tunnel_port": rp})


controller = Controller()
