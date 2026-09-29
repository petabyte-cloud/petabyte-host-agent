"""Rental-scoped TCP/UDP bridge carried inside the authenticated SSH reverse tunnel.

Only platform-declared container ports can be selected. TCP uses authenticated
SSH; native UDP uses the enrolled WireGuard peer and rental-authenticated frames.
Peers never supply an IP or host port. The legacy UDP/TCP path is refused when a
native listener is configured.
"""
import hashlib
import hmac
import json
import select
import socket
import struct
import threading
import time

MAGIC = b"PBPORT1\n"
MAX_DATAGRAM = 65507
UDP_HEADER = struct.Struct("!8sBQH16sQI")
UDP_MAGIC = b"PBUDP1\0\0"
MAX_NATIVE_DATAGRAM = MAX_DATAGRAM - UDP_HEADER.size - 32


def udp_frame(token, generation, port, session, sequence, payload, reply=False):
    """Authenticate datagrams within WireGuard; never put the rental secret on the wire."""
    if len(payload) > MAX_NATIVE_DATAGRAM:
        raise ValueError("oversize native UDP datagram")
    header = UDP_HEADER.pack(UDP_MAGIC, int(reply), generation, port, session,
                             sequence, int(time.time()))
    body = header + payload
    return body + hmac.new(token.encode(), body, hashlib.sha256).digest()


def udp_decode(data, token, generation, reply=False):
    if not UDP_HEADER.size + 32 <= len(data) <= MAX_DATAGRAM:
        raise ValueError("invalid native UDP frame")
    body, signature = data[:-32], data[-32:]
    if not hmac.compare_digest(signature, hmac.new(token.encode(), body, hashlib.sha256).digest()):
        raise ValueError("invalid native UDP authentication")
    magic, direction, lease, port, session, sequence, issued = UDP_HEADER.unpack(body[:UDP_HEADER.size])
    if (magic != UDP_MAGIC or direction != int(reply) or lease != generation
            or abs(time.time() - issued) > 15):
        raise ValueError("stale or misdirected native UDP frame")
    return port, session, sequence, body[UDP_HEADER.size:]


class ReplayWindow:
    """Allow packet reordering within 256 packets while rejecting duplicate frames."""
    def __init__(self):
        self.highest, self.bits = -1, 0

    def accept(self, sequence):
        if sequence > self.highest:
            shift = sequence - self.highest
            self.bits = ((self.bits << shift) if shift < 256 else 0) & ((1 << 256) - 1)
            self.highest = sequence
            self.bits |= 1
            return True
        distance = self.highest - sequence
        if distance >= 256 or self.bits & (1 << distance):
            return False
        self.bits |= 1 << distance
        return True


class DatagramBridge:
    """Bounded UDP-to-UDP relay, bound ONLY to the node's enrolled WireGuard address.

    Targets are fixed loopback Docker bindings. Requests must come from the gateway
    and authenticate the current rental, generation, declared service and direction.
    One connected socket per client keeps replies isolated; no TCP fallback exists.
    """
    def __init__(self, endpoints, token, generation, bind, gateway, slots, port=0):
        self.endpoints = {p: host for (p, protocol), host in endpoints.items() if protocol == "udp"}
        self.token, self.generation, self.gateway, self.slots = token, generation, gateway, slots
        self.stop, self.lock = threading.Event(), threading.Lock()
        self.sessions = {}
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.listener.bind((bind, port))
            self.listener.settimeout(.5)
        except BaseException:
            close(self.listener)
            raise
        self.port = self.listener.getsockname()[1]

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            while not self.stop.is_set():
                with self.lock:
                    for key, state in list(self.sessions.items()):
                        if time.monotonic() - state["last"] > 30:
                            close(state["socket"])
                            self.sessions.pop(key)
                            self.slots.release()
                    replies = {state["socket"]: (key, state) for key, state in self.sessions.items()}
                readable, _, _ = select.select([self.listener, *replies], [], [], .5)
                for sock in readable:
                    if self.stop.is_set():
                        return
                    try:
                        if sock is self.listener:
                            data, source = sock.recvfrom(MAX_DATAGRAM + 1)
                            if source[0] != self.gateway:
                                continue
                            port, session, sequence, payload = udp_decode(data, self.token, self.generation)
                            if port not in self.endpoints:
                                continue
                            key = (port, session, source)
                            with self.lock:
                                state = self.sessions.get(key)
                                if state is None:
                                    if not self.slots.acquire(blocking=False):
                                        continue
                                    upstream = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                                    try:
                                        upstream.connect(("127.0.0.1", self.endpoints[port]))
                                        upstream.settimeout(.5)
                                    except BaseException:
                                        close(upstream)
                                        self.slots.release()
                                        raise
                                    state = dict(socket=upstream, window=ReplayWindow(), sequence=0,
                                                 last=time.monotonic())
                                    self.sessions[key] = state
                                if not state["window"].accept(sequence):
                                    continue
                                state["last"] = time.monotonic()
                                state["socket"].send(payload)
                        else:
                            (port, session, source), state = replies[sock]
                            payload = sock.recv(MAX_NATIVE_DATAGRAM + 1)
                            frame = udp_frame(self.token, self.generation, port, session,
                                              state["sequence"], payload, reply=True)
                            state["sequence"] += 1
                            self.listener.sendto(frame, source)
                            state["last"] = time.monotonic()
                    except (OSError, ValueError):
                        continue
        except (OSError, ValueError):
            pass
        finally:
            with self.lock:
                for state in self.sessions.values():
                    close(state["socket"])
                    self.slots.release()
                self.sessions.clear()

    def shutdown(self):
        self.stop.set()
        close(self.listener)
        with self.lock:
            for state in self.sessions.values():
                close(state["socket"])


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
    def __init__(self, endpoints, token, generation, max_connections=128,
                 udp_bind=None, udp_gateway=None, udp_port=0):
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
        self.datagrams = None
        try:
            if udp_bind:
                if not udp_gateway or not any(proto == "udp" for _, proto in self.endpoints):
                    raise ValueError("native UDP needs a gateway and declared UDP service")
                self.datagrams = DatagramBridge(self.endpoints, token, generation, udp_bind,
                                                udp_gateway, self.slots, udp_port)
        except BaseException:
            close(self.listener)
            raise

    @property
    def udp_port(self):
        return self.datagrams.port if self.datagrams else None

    def start(self):
        try:
            threading.Thread(target=self._accept, daemon=True).start()
            if self.datagrams:
                self.datagrams.start()
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
            if protocol == "udp" and self.datagrams:
                return  # Native UDP rentals must never silently fall back to SSH/TCP.
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
        if self.datagrams:
            self.datagrams.shutdown()
        with self.lock:
            peers = list(self.peers)
        for peer in peers:
            close(peer)
