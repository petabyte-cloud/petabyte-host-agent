"""Rental-scoped TCP/UDP bridge carried inside the authenticated SSH reverse tunnel.

Only a platform-declared container port can be selected. The peer never supplies
an IP address or host port. UDP datagrams retain their boundaries; each client
gets its own connected UDP socket. The outer transport is SSH/TCP, so UDP can
experience head-of-line blocking during packet loss.
"""
import hmac
import json
import select
import socket
import struct
import threading
import time

MAGIC = b"PBPORT1\n"
MAX_DATAGRAM = 65507


def exact(sock, size):
    result = bytearray()
    while len(result) < size:
        part = sock.recv(size - len(result))
        if not part:
            raise EOFError("bridge closed")
        result.extend(part)
    return bytes(result)


def send_datagram(sock, data):
    if len(data) > MAX_DATAGRAM:
        raise ValueError("oversize UDP datagram")
    sock.sendall(struct.pack("!H", len(data)) + data)


def recv_datagram(sock):
    size = struct.unpack("!H", exact(sock, 2))[0]
    if size > MAX_DATAGRAM:
        raise ValueError("oversize UDP datagram")
    return exact(sock, size)


def connect(sock, token, generation, port, protocol):
    body = json.dumps(dict(token=token, generation=generation, port=port,
                           protocol=protocol), separators=(",", ":")).encode()
    if len(body) > 1024:
        raise ValueError("invalid bridge handshake")
    sock.sendall(MAGIC + struct.pack("!H", len(body)) + body)
    if exact(sock, 1) != b"\x01":
        raise PermissionError("bridge rejected service")


def close(sock):
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()


def splice(left, right, stop=None, on_reply=None, idle_seconds=7200):
    last = time.monotonic()
    while stop is None or not stop.is_set():
        if time.monotonic() - last > idle_seconds:
            break
        readable, _, _ = select.select([left, right], [], [], 1)
        for source in readable:
            data = source.recv(65536)
            if not data:
                return
            (right if source is left else left).sendall(data)
            last = time.monotonic()
            if source is right and on_reply:
                on_reply()
                on_reply = None


class Bridge:
    def __init__(self, endpoints, token, generation, max_connections=128):
        if not isinstance(token, str) or not 32 <= len(token) <= 128:
            raise ValueError("bridge needs a rental credential")
        if type(generation) is not int or generation < 0:
            raise ValueError("invalid bridge assignment")
        self.token = token.encode()
        self.generation = generation
        self.endpoints = {}
        for endpoint in endpoints:
            port, host = endpoint["container_port"], endpoint["host_port"]
            protocol = endpoint["protocol"]
            if (type(port) is not int or type(host) is not int or not 1 <= port <= 65535
                    or not 1 <= host <= 65535 or protocol not in ("tcp", "udp")):
                raise ValueError("invalid bridge endpoint")
            key = (port, protocol)
            if key in self.endpoints:
                raise ValueError("duplicate bridge endpoint")
            self.endpoints[key] = host
        if not self.endpoints or len(self.endpoints) > 33:
            raise ValueError("bridge needs 1-32 public services and an optional private primary service")
        self.stop = threading.Event()
        self.slots = threading.BoundedSemaphore(max_connections)
        self.peers = set()
        self.lock = threading.Lock()
        self.listener = socket.socket()
        try:
            self.listener.bind(("127.0.0.1", 0))
            self.listener.listen(32)
            self.listener.settimeout(1)
        except OSError:
            close(self.listener)
            raise
        self.port = self.listener.getsockname()[1]

    def start(self):
        try:
            threading.Thread(target=self._accept, daemon=True).start()
        except RuntimeError:
            self.shutdown()
            raise
        return self

    def _accept(self):
        while not self.stop.is_set():
            try:
                peer, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            if not self.slots.acquire(blocking=False):
                close(peer)
                continue
            with self.lock:
                self.peers.add(peer)
            try:
                threading.Thread(target=self._handle, args=(peer,), daemon=True).start()
            except RuntimeError:
                close(peer)
                with self.lock:
                    self.peers.discard(peer)
                self.slots.release()

    def _handle(self, peer):
        upstream = None
        try:
            peer.settimeout(5)
            if exact(peer, len(MAGIC)) != MAGIC:
                return
            size = struct.unpack("!H", exact(peer, 2))[0]
            if not 1 <= size <= 1024:
                return
            request = json.loads(exact(peer, size))
            if not isinstance(request, dict):
                return
            token = request.get("token")
            if (not isinstance(token, str) or not hmac.compare_digest(token.encode(), self.token)
                    or type(request.get("generation")) is not int
                    or request["generation"] != self.generation
                    or type(request.get("port")) is not int):
                return
            protocol = request.get("protocol")
            host_port = self.endpoints.get((request["port"], protocol))
            if not host_port:
                return
            if protocol == "tcp":
                upstream = socket.create_connection(("127.0.0.1", host_port), timeout=5)
            else:
                upstream = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                upstream.connect(("127.0.0.1", host_port))
            upstream.settimeout(5)
            peer.sendall(b"\x01")
            if protocol == "tcp":
                peer.settimeout(30)
                upstream.settimeout(30)
                splice(peer, upstream, self.stop)
            else:
                last = time.monotonic()
                while not self.stop.is_set() and time.monotonic() - last < 30:
                    readable, _, _ = select.select([peer, upstream], [], [], 1)
                    for source in readable:
                        if source is peer:
                            upstream.send(recv_datagram(peer))
                        else:
                            send_datagram(peer, upstream.recv(MAX_DATAGRAM))
                        last = time.monotonic()
        except (OSError, EOFError, ValueError, TypeError):
            pass
        finally:
            close(peer)
            close(upstream)
            with self.lock:
                self.peers.discard(peer)
            self.slots.release()

    def shutdown(self):
        self.stop.set()
        close(self.listener)
        with self.lock:
            peers = list(self.peers)
        for peer in peers:
            close(peer)
