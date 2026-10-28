"""Inference-only authenticated edge for the optional Lucebox container.

Copied into the pinned runtime by the seller agent. The native server stays on
container loopback; only this edge's port is published. Uses Python's stdlib.
"""
from __future__ import annotations

import hmac
import http.client
import json
import math
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = "qwen3-0.6b-bf16"
MODEL_PATH = "/opt/lucebox-hub/server/models/Qwen3-0.6B-BF16.gguf"
NATIVE = "/opt/lucebox-hub/server/build/luce_server"
BODY_LIMIT = 65536
RESPONSE_LIMIT = 2 * 1024 * 1024
INFERENCE_TIMEOUT = 180
IDLE_TIMEOUT = 30


@dataclass(frozen=True)
class Config:
    key: str
    context: int

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        key = env.get("PETABYTE_LUCEBOX_KEY", "")
        value = env.get("PETABYTE_LUCEBOX_CONTEXT", "1024")
        if not 32 <= len(key) <= 256 or any(ord(c) < 33 or ord(c) > 126 for c in key):
            raise ValueError("Lucebox endpoint credential is missing or invalid")
        if not value.isascii() or not value.isdigit() or not 512 <= int(value) <= 2048:
            raise ValueError("Lucebox context must be between 512 and 2048")
        if env.get("PETABYTE_LUCEBOX_MODEL") != MODEL or env.get("LUCE_TARGET") != MODEL_PATH:
            raise ValueError("Lucebox requires the pinned Qwen3-0.6B BF16 profile")
        return cls(key=key, context=int(value))

    def command(self):
        # Never forward buyer-supplied flags, LUCE_ARGS, profiles or draft paths.
        return [NATIVE, MODEL_PATH, "--host", "127.0.0.1", "--port", "8081",
                "--target-device", "cuda:0", "--max-ctx", str(self.context),
                "--model-name", MODEL, "--chunk", "128",
                "--prefix-cache-slots", "0", "--prefill-cache-slots", "0",
                "--default-max-tokens", "64", "--hard-limit-reply-budget", "256",
                "--think-max-tokens", "0", "--no-tool-checks"]


def native_environment(env=None):
    env = os.environ if env is None else env
    names = ("PATH", "LD_LIBRARY_PATH", "HOME", "TMPDIR", "LANG", "LC_ALL",
             "NVIDIA_VISIBLE_DEVICES", "NVIDIA_DRIVER_CAPABILITIES",
             "CUDA_VISIBLE_DEVICES", "CUDA_MODULE_LOADING")
    return {name: env[name] for name in names if name in env}


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON field")
        value[key] = item
    return value


def _constant(_):
    raise ValueError("Non-finite JSON number")


def validate_request(raw, context):
    data = json.loads(raw, object_pairs_hook=_object, parse_constant=_constant)
    allowed = {"model", "messages", "stream", "max_tokens", "temperature",
               "top_p", "top_k", "seed", "stop"}
    if not isinstance(data, dict) or set(data) - allowed:
        raise ValueError("Unsupported inference setting")
    if data.get("model") != MODEL:
        raise ValueError("Request model must match the deployed model")
    messages = data.get("messages")
    if not isinstance(messages, list) or not 1 <= len(messages) <= 32:
        raise ValueError("Provide between 1 and 32 text messages")
    text_size = 0
    for msg in messages:
        if (not isinstance(msg, dict) or set(msg) != {"role", "content"}
                or msg["role"] not in ("system", "user", "assistant")
                or not isinstance(msg["content"], str)):
            raise ValueError("Only system, user and assistant text messages are supported")
        text_size += len(msg["content"])
    if text_size > context * 8:
        raise ValueError("Message text exceeds this deployment's request limit")
    # The native tokenizer enforces the actual prompt + completion context.
    tokens = data.setdefault("max_tokens", min(64, context // 2))
    if type(tokens) is not int or not 1 <= tokens <= min(256, context // 2):
        raise ValueError("max_tokens exceeds the deployment limit")
    if type(data.get("stream", False)) is not bool:
        raise ValueError("stream must be a boolean")
    for name, low, high in (("temperature", 0, 2), ("top_p", 0, 1)):
        if name in data and (type(data[name]) not in (int, float)
                             or not math.isfinite(data[name]) or not low <= data[name] <= high):
            raise ValueError(name + " is outside its allowed range")
    for name, maximum in (("top_k", 100), ("seed", 2**31 - 1)):
        if name in data and (type(data[name]) is not int or not 0 <= data[name] <= maximum):
            raise ValueError(name + " is outside its allowed range")
    if "stop" in data:
        stops = [data["stop"]] if isinstance(data["stop"], str) else data["stop"]
        if not isinstance(stops, list) or not 1 <= len(stops) <= 4 or any(
                not isinstance(s, str) or not 1 <= len(s) <= 64 for s in stops):
            raise ValueError("stop must contain at most four short strings")
    return json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")


class GateServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True

    def __init__(self, address, config, upstream_port=8081):
        super().__init__(address, GateHandler)
        self.config = config
        self.upstream_port = upstream_port
        self.inference = threading.BoundedSemaphore(1)
        self.stopping = threading.Event()
        self.native = None


class GateHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "PetabyteInference"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *_):
        # Never log Authorization, request bodies, prompts, or upstream URLs.
        pass

    def _json(self, status, message):
        body = json.dumps(message).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def _error(self, status, message):
        self._json(status, {"error": {"message": message}})

    def _check(self):
        self.close_connection = True
        if self.server.stopping.is_set():
            self._error(503, "Deployment is stopping")
            return False
        if sum(len(k) + len(v) for k, v in self.headers.items()) > 16384:
            self._error(431, "Request headers are too large")
            return False
        if (self.headers.get_all("Transfer-Encoding") or self.headers.get_all("Trailer")
                or self.headers.get_all("Expect") or self.headers.get_all("Content-Encoding")
                or len(self.headers.get_all("Content-Length", [])) > 1):
            self._error(400, "Unsupported HTTP framing")
            return False
        auth = self.headers.get_all("Authorization", [])
        expected = "Bearer " + self.server.config.key
        if len(auth) != 1 or not hmac.compare_digest(auth[0].encode(), expected.encode()):
            self._error(401, "A valid deployment bearer token is required")
            return False
        return True

    def handle_expect_100(self):
        self.close_connection = True
        self._error(417, "Expect is not supported")
        return False

    def do_GET(self):
        if not self._check():
            return
        if self.path not in ("/v1/models", "/health"):
            self._error(404, "Unknown inference endpoint")
            return
        if self.headers.get("Content-Length", "0") != "0":
            self._error(400, "GET requests cannot have a body")
            return
        conn = http.client.HTTPConnection("127.0.0.1", self.server.upstream_port, timeout=5)
        try:
            conn.request("GET", "/v1/models", headers={"Connection": "close"})
            upstream_socket = conn.sock
            response = conn.getresponse()
            raw = response.read(RESPONSE_LIMIT + 1)
            body = json.loads(raw)
            if (response.status != 200 or len(raw) > RESPONSE_LIMIT
                    or not isinstance(body, dict) or not isinstance(body.get("data"), list)
                    or [m.get("id") for m in body["data"] if isinstance(m, dict)] != [MODEL]):
                raise ValueError("Expected model is not loaded")
            if self.path == "/health":
                self._json(200, {"status": "ok", "model": MODEL})
            else:
                # Serve a minimal discovery document without native build or host metadata.
                self._json(200, {"object": "list", "data": [
                    {"id": MODEL, "object": "model", "owned_by": "deployment"}]})
        except (OSError, ValueError, http.client.HTTPException):
            self._error(503, "Model is loading or the inference process is unavailable")
        finally:
            conn.close()

    def do_POST(self):
        if not self._check():
            return
        if self.path != "/v1/chat/completions":
            self._error(404, "Unknown inference endpoint")
            return
        length = self.headers.get("Content-Length", "")
        if not length.isascii() or not length.isdigit() or not 1 <= int(length) <= BODY_LIMIT:
            self._error(413, "Provide a bounded Content-Length request body")
            return
        if self.headers.get_content_type() != "application/json":
            self._error(415, "Content-Type must be application/json")
            return
        try:
            raw = self.rfile.read(int(length))
            if len(raw) != int(length):
                raise ValueError("Incomplete request body")
            body = validate_request(raw, self.server.config.context)
        except (OSError, ValueError, TypeError, UnicodeError):
            self._error(400, "Invalid request or unsupported inference settings")
            return
        if not self.server.inference.acquire(blocking=False):
            self._error(429, "This deployment supports one inference request at a time")
            return
        conn = http.client.HTTPConnection("127.0.0.1", self.server.upstream_port, timeout=IDLE_TIMEOUT)
        sent = False
        upstream_socket = None
        response = None
        deadline = time.monotonic() + INFERENCE_TIMEOUT
        try:
            conn.request("POST", "/v1/chat/completions", body=body, headers={
                "Content-Type": "application/json", "Connection": "close"})
            upstream_socket = conn.sock
            response = conn.getresponse()
            if not 200 <= response.status < 600 or 300 <= response.status < 400:
                raise ValueError("Unexpected upstream status")
            content_type = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type not in ("application/json", "text/event-stream"):
                raise ValueError("Unexpected upstream content type")
            self.send_response(response.status)
            self.send_header("Content-Type", content_type)
            self.send_header("Connection", "close")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            sent = True
            total = 0
            while not self.server.stopping.is_set():
                if time.monotonic() >= deadline:
                    raise TimeoutError("Inference deadline exceeded")
                if upstream_socket is not None:
                    upstream_socket.settimeout(min(IDLE_TIMEOUT, max(0.1, deadline - time.monotonic())))
                chunk = response.read1(16384)
                if not chunk:
                    break
                total += len(chunk)
                if total > RESPONSE_LIMIT:
                    raise ValueError("Inference response exceeds limit")
                self.wfile.write(chunk)
                self.wfile.flush()
        except (OSError, ValueError, http.client.HTTPException):
            if not sent:
                self._error(502, "Inference process failed or timed out")
            # Once a stream began, close it instead of appending a success/error body.
        finally:
            # Shutdown, rather than just closing the HTTPResponse, propagates a
            # buyer disconnect/deadline to native cancellation even mid-stream.
            if upstream_socket is not None:
                try:
                    upstream_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            if response is not None:
                response.close()
            conn.close()
            self.server.inference.release()
            self.close_connection = True

    def _unsupported(self):
        if self._check():
            self._error(405, "Method is not allowed")

    do_HEAD = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_CONNECT = _unsupported


def main():
    config = Config.from_env()
    server = GateServer(("0.0.0.0", 8080), config)
    native = subprocess.Popen(config.command(), env=native_environment(), close_fds=True)
    server.native = native

    def stop(*_):
        server.stopping.set()
        if native.poll() is None:
            native.terminate()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    def watch():
        code = native.wait()
        if not server.stopping.is_set():
            message = ('Lucebox: native inference process exited unexpectedly (exit ' + str(code)
                       + '). Inspect CUDA/model loading errors; verify assigned VRAM and available RAM.')
            if code == -9:
                message += ' SIGKILL may indicate host memory exhaustion or an external stop.'
            print(message, file=sys.stderr, flush=True)
        stop()

    threading.Thread(target=watch, daemon=True).start()
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.stopping.set()
        server.server_close()
        if native.poll() is None:
            native.terminate()
            try:
                native.wait(timeout=15)
            except subprocess.TimeoutExpired:
                native.kill()
                native.wait(timeout=5)
    return native.returncode or 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError):
        raise SystemExit("Lucebox inference edge could not start; check the pinned profile and endpoint credential")
