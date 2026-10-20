"""Several managed CUDA servers on one opted-in GPU; exclusive jobs still stop every server.

This uses ordinary CUDA contexts. It neither installs MPS/vGPU nor claims hardware isolation.
"""
import hmac
import http.server
import ipaddress
import json
import os
import re
import secrets
import socket
import socketserver
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
from model3d_scene import sampling_options

WORKER_RE = re.compile(r"inf-[0-9a-f]{16}\Z")
GPU_RE = re.compile(r"GPU-[a-fA-F0-9-]+\Z")
CODER_MODEL = "qwen2.5-coder-7b-instruct"


def supported():
    """Default-on for native Linux NVIDIA. Explicit false, WSL and other GPUs keep legacy serving."""
    if os.getenv("PETABYTE_SHARED_POOL", "true").lower() != "true" or sys.platform != "linux":
        return False
    try:
        if "microsoft" in Path("/proc/sys/kernel/osrelease").read_text().lower():
            return False
    except OSError:
        return False
    import gpu_runtime
    return gpu_runtime.vendor() == "nvidia"


def snapshot():
    """Use physical inventory, never aggregate multiple cards into one fake memory budget."""
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=uuid,memory.total,memory.free",
                            "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5, check=False)
        rows = r.stdout.strip().splitlines()
        if r.returncode or len(rows) != 1:
            return None
        uid, total, free = (x.strip() for x in rows[0].split(","))
        if not GPU_RE.fullmatch(uid):
            return None
        total, free = int(float(total)), int(float(free))
        if not 0 < free <= total <= 1024 ** 2:
            return None
        memory = {line.split(":", 1)[0]: int(line.split()[1]) // 1024
                  for line in Path("/proc/meminfo").read_text().splitlines()
                  if line.startswith(("MemTotal:", "MemAvailable:"))}
        return {"uuid": uid, "total": total, "free": free,
                "host_total": memory["MemTotal"], "host_free": memory["MemAvailable"]}
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return None


def valid_plan(p, gpu):
    import inference_worker as iw
    if not isinstance(p, dict) or p.get("version") != 1 or not isinstance(p.get("workers"), list):
        return False
    workers = p["workers"]
    if not 2 <= len(workers) <= 3 or not gpu:
        return False
    budget = p.get("gpu_budget_mib")
    headroom = max(2048, int(gpu["total"] * 0.1))
    if type(budget) is not int or not 0 < budget <= gpu["total"] - headroom:
        return False
    ids, models, total = set(), set(), 0
    for w in workers:
        if (not isinstance(w, dict) or not iw.valid(w) or w.get("shared") is not True
                or w.get("offload") is True or not isinstance(w.get("worker_id"), str)
                or not WORKER_RE.fullmatch(w["worker_id"]) or w["worker_id"] in ids
                or w["model"] in models):
            return False
        vram, ram = w.get("reserved_vram_mib"), w.get("reserved_ram_mib")
        if type(vram) is not int or type(ram) is not int or not 0 < vram <= budget or not 0 < ram <= 1024 ** 2:
            return False
        ids.add(w["worker_id"])
        models.add(w["model"])
        total += vram
    return (total <= budget and sum(w.get("kind") is None for w in workers) == 1
            and sum(w["reserved_ram_mib"] for w in workers) + 6144 <= gpu.get("host_total", 0))


class PoolController:
    def __init__(self):
        import inference_worker as iw
        self.legacy = iw.Controller()
        self.lock = threading.RLock()
        self.children, self.permits = {}, {}
        self.generation, self.busy, self.shared_job = 0, False, None
        self.open_tunnel = lambda port, worker="inference": None
        self.close_tunnel = lambda worker="inference": None
        self.tunnel_alive = lambda worker="inference": False
        self.cooldown_until = 0.0

    def attach(self, open_tunnel, close_tunnel, tunnel_alive):
        self.open_tunnel, self.close_tunnel, self.tunnel_alive = open_tunnel, close_tunnel, tunnel_alive
        self.legacy.attach(lambda port: open_tunnel(port), lambda: close_tunnel(), lambda: tunnel_alive())

    def enabled(self):
        return self.legacy.enabled()

    def ticket(self):
        with self.lock:
            return self.generation

    def report(self):
        if not self.enabled():
            return None
        with self.lock:
            if self.children:
                return {"version": 1, "workers": [{**c.report(), "worker_id": key}
                                                  for key, c in self.children.items()]}
            return self.legacy.report()

    def stop(self):
        with self.lock:
            self.legacy.stop()
            for c in self.children.values():
                c.stop()
            self.children.clear()
            self.permits.clear()

    def before_work(self, task=None):
        """Only a server-authorized CPU-only 3D builder may keep the managed servers running."""
        with self.lock:
            self.generation += 1
            task = task or {}
            if self.endpoint_for_3d(task):
                self.shared_job = task["task_id"]
                return True
            self.busy = True
            self.stop()
            if task.get("gpu"):
                import inference_worker as iw
                r = iw.docker("ps", "--filter", f"label={iw.LABEL}", "--format", "{{.Names}}")
                return r.returncode == 0 and not any(
                    re.fullmatch(r"petabyte-inference(?:-inf-[0-9a-f]{16})?", name)
                    for name in r.stdout.splitlines())
            return True

    def after_work(self):
        with self.lock:
            self.busy, self.shared_job = False, None
            self.legacy.after_work()

    def serving(self):
        with self.lock:
            return bool(self.children or self.legacy._thread)

    def endpoint_for_3d(self, task):
        if (task.get("task_type") != "model3d" or task.get("gpu") is not False
                or not isinstance(task.get("shared_pool_worker"), str)):
            return None
        key = task["shared_pool_worker"]
        c, p = self.children.get(key), self.permits.get(key)
        if (not c or not p or p["model"] != CODER_MODEL
                or (task.get("model") or {}).get("sha256") != p.get("sha256")
                or c.report().get("state") != "warm" or c._lapsed(c._stop)):
            return None
        return c.endpoint

    def heartbeat(self, ticket, permit, live=False):
        import inference_worker as iw
        with self.lock:
            if ticket != self.generation:
                return
            if not permit or self.busy or live or not self.enabled():
                self.stop()
                return
            if "workers" not in permit:
                for c in self.children.values():
                    c.stop()
                self.children.clear()
                self.permits.clear()
                self.legacy.heartbeat(self.legacy.ticket(), permit, live=live)
                return
            gpu = snapshot() if supported() else None
            if not valid_plan(permit, gpu):
                self.stop()
                return
            if time.monotonic() < self.cooldown_until:
                return
            # Loaded workers use their reservations during generation. Full planning headroom
            # is an admission margin; only an absolute emergency floor drains an active group.
            floor = 256 if self.children else max(2048, int(gpu["total"] * 0.1))
            if gpu["free"] < floor:
                self.stop()
                self.cooldown_until = time.monotonic() + 120
                return
            self.legacy.stop()
            # Admit the group's future peaks alongside existing GPU clients, including an older
            # agent that is being drained after a reinstall. Check again after stopping legacy.
            if not self.children:
                refreshed = snapshot()
                required = sum(p["reserved_vram_mib"] for p in permit["workers"])
                if (not refreshed or refreshed["uuid"] != gpu["uuid"]
                        or refreshed["free"] < required + max(2048, int(gpu["total"] * 0.1))):
                    return
                gpu = refreshed
            wanted = {p["worker_id"]: p for p in permit["workers"]}
            # Stop retired/changed allocations BEFORE admitting replacements; no transient overcommit.
            for key, c in list(self.children.items()):
                if key not in wanted or iw._lease_key(wanted[key]) != iw._lease_key(self.permits[key]):
                    c.stop()
                    del self.children[key]
            self.permits = wanted
            starting = any(c.report().get("state") in ("downloading", "starting") for c in self.children.values())
            for key, p in wanted.items():
                c = self.children.get(key)
                if c is None:
                    # Serialize warm-up and check real free VRAM as well as the reserved plan.
                    if (starting or gpu["free"] < p["reserved_vram_mib"] + max(2048, int(gpu["total"] * 0.1))
                            or gpu.get("host_free", 0) < p["reserved_ram_mib"] + 6144):
                        continue
                    c = iw.Controller(name=f"{iw.NAME}-{key}", gpu_args=["--gpus", f"device={gpu['uuid']}"], shared=True)
                    tunnel = key + "-" + secrets.token_hex(4)
                    c.attach(lambda port, k=tunnel: self.open_tunnel(port, k),
                             lambda k=tunnel: self.close_tunnel(k), lambda k=tunnel: self.tunnel_alive(k))
                    self.children[key] = c
                    starting = True
                c.heartbeat(c.ticket(), p)


class Model3DProxy:
    """Per-build authenticated chat-only bridge. The Blender container never gets the pool key,
    a GPU device, arbitrary upstream paths or the shared model server's management endpoints."""
    def __init__(self, gateway, endpoint=None, active=lambda: True, *, forward=None,
                 max_tokens=2048, max_chars=24000, max_calls=8, model="local", unix_socket=None):
        if unix_socket is not None:
            if gateway is not None or endpoint is not None or forward is None or not os.path.isabs(unix_socket):
                raise ValueError("the private relay requires only its own Unix forwarder")
        else:
            host = ipaddress.ip_address(gateway)
            if host.version != 4 or not host.is_private or host.is_loopback or host.is_link_local:
                raise ValueError("the private build network has no usable gateway")
        self.key = secrets.token_urlsafe(32)
        target, upstream_key = endpoint if endpoint else (None, None)
        self.closed = threading.Event()
        self.client = httpx.Client(trust_env=False, timeout=600)
        client = self.client
        key, quota, lock, closed = self.key, [max_calls], threading.Lock(), self.closed
        sampling = sampling_options(model)

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, code, body):
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                if closed.is_set() or not active():
                    return self.reply(503, b'{"error":"build coder lease ended"}')
                auth = self.headers.get("Authorization", "")
                if self.path != "/v1/chat/completions" or not hmac.compare_digest(auth.encode(), f"Bearer {key}".encode()):
                    return self.reply(403, b'{"error":"forbidden"}')
                try:
                    n = int(self.headers.get("Content-Length", "0"))
                    # JSON may escape each content byte as six ASCII bytes (e.g. control chars).
                    if not 0 < n <= max(128 * 1024, 6 * max_chars + 16 * 1024):
                        return self.reply(413, b'{"error":"request too large"}')
                    body = json.loads(self.rfile.read(n))
                    messages = body["messages"]
                    if (not isinstance(messages, list) or not 1 <= len(messages) <= 32
                            or any(not isinstance(m, dict) or m.get("role") not in ("system", "user", "assistant")
                                   or not isinstance(m.get("content"), str) for m in messages)
                            or sum(len(m["content"].encode()) for m in messages) > max_chars):
                        return self.reply(400, b'{"error":"invalid conversation"}')
                except (ValueError, KeyError, TypeError, OSError):
                    return self.reply(400, b'{"error":"invalid request"}')
                with lock:
                    if quota[0] <= 0:
                        return self.reply(429, b'{"error":"build request limit"}')
                    quota[0] -= 1
                try:
                    payload = {"model": "local", "messages": [{"role": m["role"], "content": m["content"]}
                                                               for m in messages], "max_tokens": max_tokens,
                               **sampling, "stream": False}
                    r = (forward(payload) if forward else client.post(target + "/v1/chat/completions",
                         headers={"Authorization": f"Bearer {upstream_key}"}, json=payload))
                    if len(r.content) > 512 * 1024:
                        return self.reply(502, b'{"error":"local coder response too large"}')
                    return self.reply(r.status_code, r.content)
                except (httpx.HTTPError, RuntimeError):
                    return self.reply(502, b'{"error":"shared coder unavailable"}')

            def do_GET(self):
                if self.path == "/health" and not closed.is_set() and active():
                    return self.reply(200, b'{"status":"ok"}')
                return self.reply(403, b'{"error":"forbidden"}')

        class BoundedRequests:
            """Generated code cannot create unlimited host threads with partial HTTP requests."""
            def __init__(self, address):
                self.connections = threading.BoundedSemaphore(2)
                super().__init__(address, Handler)

            def get_request(self):
                connection, address = super().get_request()
                connection.settimeout(15)
                return connection, address

            def process_request(self, request, address):
                if not self.connections.acquire(blocking=False):
                    self.shutdown_request(request)
                    return
                try:
                    super().process_request(request, address)
                except Exception:  # release the admission ticket on a failed thread start
                    self.connections.release()
                    raise

            def process_request_thread(self, request, address):
                try:
                    super().process_request_thread(request, address)
                finally:
                    self.connections.release()

        class Server(BoundedRequests, http.server.ThreadingHTTPServer):
            pass

        class UnixServer(BoundedRequests, socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            pass

        self.unix_socket = unix_socket
        self.server = UnixServer(unix_socket) if unix_socket else Server((gateway, 0))
        self.server.daemon_threads = True
        # Only the trusted, non-root relay container mounts this task-specific directory.
        # Its read-only mount permits connecting, never replacing/unlinking the socket. Every
        # chat still needs the existing per-build key, quota and current prepaid task lease.
        if unix_socket:
            os.chmod(unix_socket, 0o666)
        self.port = None if unix_socket else self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True, name="pb-3d-pool-proxy").start()

    def close(self):
        self.closed.set()
        self.client.close()
        self.server.shutdown()
        self.server.server_close()
        if self.unix_socket:
            Path(self.unix_socket).unlink(missing_ok=True)
