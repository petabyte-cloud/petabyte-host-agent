"""Bounded byte transport from one private job-network peer to its host Unix socket.

This trusted sidecar has no node credential, upstream URL, model, GPU or public port.
The existing host Model3DProxy remains the authenticated chat/lease/quota boundary.
Blender reaches this peer's exact network IP without accessing a host gateway.
"""
import select
import socket
import socketserver
import time

MAX_CONNECTIONS = 2
MAX_BYTES = 1024 * 1024  # escaped 90k-byte requests and bounded 512KiB replies plus headers
MAX_SECONDS = 620


def wait_ready(url, active, seconds=60):
    """Agent-only probe of the exact job peer before starting the buyer's Blender."""
    import httpx

    deadline = time.monotonic() + seconds
    with httpx.Client(trust_env=False, timeout=2) as client:
        while active() and time.monotonic() < deadline:
            try:
                response = client.get(url + "/health")
                if response.status_code == 200 and response.json() == {"status": "ok"}:
                    return
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(0.2)
    raise RuntimeError("the private job relay did not become ready")


def serve(unix_socket, host="0.0.0.0", port=8080):
    import threading

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            with socket.socket(socket.AF_UNIX) as upstream:
                upstream.settimeout(15)
                try:
                    upstream.connect(unix_socket)
                    self.request.settimeout(15)
                    peers = {self.request: upstream, upstream: self.request}
                    counts = {self.request: 0, upstream: 0}
                    readers = list(peers)
                    deadline = time.monotonic() + MAX_SECONDS
                    received = False
                    while readers and time.monotonic() < deadline:
                        ready, _, _ = select.select(readers, [], [], 15)
                        if not ready:
                            if not received:  # partial/idle request admission is not an inference wait
                                return
                            continue
                        for source in ready:
                            data = source.recv(65536)
                            if not data:
                                readers.remove(source)
                                try:
                                    peers[source].shutdown(socket.SHUT_WR)
                                except OSError:
                                    pass
                                continue
                            received = True
                            counts[source] += len(data)
                            if counts[source] > MAX_BYTES:
                                return
                            try:
                                peers[source].sendall(data)
                            except OSError:
                                # A chat-only forwarder can reject headers before reading a
                                # body. Preserve its pending HTTP error in the other direction.
                                readers.remove(source)
                except (OSError, ValueError):
                    return  # no request, response, credential or socket path in logs

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

        def __init__(self):
            self.connections = threading.BoundedSemaphore(MAX_CONNECTIONS)
            super().__init__((host, port), Handler)

        def process_request(self, request, address):
            if not self.connections.acquire(blocking=False):
                self.shutdown_request(request)
                return
            try:
                super().process_request(request, address)
            except Exception:
                self.connections.release()
                raise

        def process_request_thread(self, request, address):
            try:
                super().process_request_thread(request, address)
            finally:
                self.connections.release()

    return Server()


if __name__ == "__main__":
    # The pinned Blender image supplies its bundled Python. No bpy code, art generation,
    # weights, dependencies or additional image are loaded by this control-plane script.
    with serve("/relay/bridge.sock") as server:
        server.serve_forever()
