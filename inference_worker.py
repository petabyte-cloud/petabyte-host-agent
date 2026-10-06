"""Serve the community inference pool while this GPU is idle (API side: lumaris_api/inference_pool.py).

Every heartbeat the server may grant a short lease (`inference_worker` in the reply) naming one pinned
model. Under it we download the GGUF once (sha256-verified, resumable), run the pinned llama.cpp
server on 127.0.0.1 with a fresh random API key, open our reverse tunnel to the gateway, and report
`warm` in the next heartbeat. A claimed job, a live rental, a lapsed lease or the owner's veto
(PETABYTE_INFERENCE_WORKER=false) stops it: a rental always gets the GPU first.

Nothing the server sends can make this run something else: the image must be the official llama.cpp
repository pinned by digest, the weights must come from huggingface.co and match the pinned hash.
"""
import hashlib
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
READY_TIMEOUT_S = 300
_IMAGE_RE = re.compile(r"^ghcr\.io/ggml-org/llama\.cpp@sha256:[0-9a-f]{64}$")
_URL_RE = re.compile(r"^https://huggingface\.co/[A-Za-z0-9._/-]+\.gguf$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_MODEL_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


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


def valid(p):
    """The lease's shape, and that it names only the allowed image and weights."""
    def num(v, lo, hi):
        return type(v) is int and lo <= v <= hi
    return (isinstance(p, dict) and isinstance(p.get("model"), str) and _MODEL_RE.match(p["model"])
            and isinstance(p.get("url"), str) and _URL_RE.match(p["url"])
            and isinstance(p.get("sha256"), str) and _SHA_RE.match(p["sha256"])
            and isinstance(p.get("image"), str) and _IMAGE_RE.match(p["image"])
            and num(p.get("size"), 1, 200 * 1024 ** 3) and num(p.get("ctx"), 512, 131072)
            and num(p.get("parallel"), 1, 8) and num(p.get("seconds"), 1, 30))


def fetch_model(p, cancelled=lambda: False):
    """The pinned GGUF on local disk, verified. Resumes a partial download; keeps one model only."""
    MODELS.mkdir(parents=True, exist_ok=True)
    path, part = MODELS / f"{p['sha256']}.gguf", MODELS / f"{p['sha256']}.part"
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
    headers = {"User-Agent": "petabyte-agent", **({"Range": f"bytes={have}-"} if have else {})}
    with httpx.stream("GET", p["url"], headers=headers, follow_redirects=True, timeout=60,
                      trust_env=False) as r:
        r.raise_for_status()
        if have and r.status_code != 206:                 # server ignored the range: start over
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
    if have != p["size"] or h.hexdigest() != p["sha256"]:
        part.unlink(missing_ok=True)
        raise RuntimeError("inference model failed its hash check; discarded")
    os.replace(part, path)
    for old in MODELS.iterdir():
        if old != path:
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
        self._until, self._retry_at = 0.0, 0.0

    def attach(self, open_tunnel, close_tunnel, tunnel_alive):
        self.open_tunnel, self.close_tunnel, self.tunnel_alive = open_tunnel, close_tunnel, tunnel_alive

    def enabled(self):
        """On by default; PETABYTE_INFERENCE_WORKER=false vetoes it. PETABYTE_IDLE_MINING is not a
        signal here: the installer writes it =true on every node, so it says nothing about the owner."""
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
        with self.lock:
            self.generation += 1
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
            self._until = time.monotonic() + permit["seconds"]
            key = (permit["model"], permit["sha256"], permit["ctx"], permit["parallel"], permit["image"])
            if self._thread and self._thread.is_alive() and self._key == key:
                return                                    # lease renewed; already serving it
            if time.monotonic() < self._retry_at:
                return
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
            path = fetch_model(p, lambda: self._lapsed(stop))
            if docker("image", "inspect", p["image"]).returncode:
                if docker("pull", p["image"], timeout=1800).returncode:
                    raise RuntimeError("could not pull the inference server image")
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
                       p["image"], "-m", "/models/model.gguf", "--host", "0.0.0.0", "--port", "8080",
                       "-c", str(p["ctx"] * p["parallel"]), "-np", str(p["parallel"]), "-ngl", "999",
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
            rp = self.open_tunnel(port)
            if not rp:
                raise RuntimeError("could not open the reverse tunnel to the gateway")
            warm = dict(state="warm", model=p["model"], ctx=p["ctx"], parallel=p["parallel"],
                        tunnel_port=rp, gateway=self.gateway, key=key, bench_tps=tps)
            self._set(**warm)
            logging.info("inference worker warm: %s at %.1f tok/s", p["model"], tps)
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
        except Cancelled:
            pass
        except Exception as e:                            # noqa: BLE001 — report it, retry later
            failed = True
            logging.warning("inference worker: %s", e)
            self._set(state="error", model=p["model"], error=str(e)[:200])
            self._retry_at = time.monotonic() + RETRY_AFTER_ERROR_S
        finally:
            # stop() already cleaned up (and a newer worker may own the container by now); only a
            # worker that ended on its own — lease lapsed or failed — tidies up after itself.
            if not stop.is_set():
                try:
                    self.close_tunnel()
                    stop_container()
                except Exception:                         # noqa: BLE001
                    pass
                if not failed:
                    self._set(state="off")


controller = Controller()
