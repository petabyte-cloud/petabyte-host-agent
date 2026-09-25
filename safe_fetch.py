"""Bounded public-HTTP fetch with DNS pinned to the actual socket.

The host performs this request, so Docker egress rules are not a substitute.
No proxies, cookies, URL credentials, decompression or pooled connections are used.
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import queue
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

MAX_BYTES = 8 * 1024 * 1024
_DNS_SLOTS = threading.BoundedSemaphore(4)
_TRANSITION = tuple(map(ipaddress.ip_network, (
    "64:ff9b::/96", "64:ff9b:1::/48", "2001::/32", "2002::/16",
)))


class FetchDenied(ValueError):
    pass


def public_ip(value):
    try:
        addr = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    if "%" in value:
        return False
    if getattr(addr, "ipv4_mapped", None):
        addr = addr.ipv4_mapped
    return addr.is_global and not addr.is_multicast and not any(
        addr.version == net.version and addr in net for net in _TRANSITION)


def _remaining(deadline):
    left = deadline - time.monotonic()
    if left <= 0:
        raise FetchDenied("notebook download deadline exceeded")
    return left


def _resolve(host, port, deadline):
    # libc DNS has no timeout argument. Bound caller wait and outstanding resolver threads;
    # a wedged resolver cannot accumulate unbounded threads or prolong a workload indefinitely.
    if not _DNS_SLOTS.acquire(blocking=False):
        raise FetchDenied("notebook resolver unavailable")
    result = queue.Queue(maxsize=1)

    def resolve():
        try:
            result.put(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM,
                                          proto=socket.IPPROTO_TCP))
        except Exception:
            result.put(None)
        finally:
            _DNS_SLOTS.release()

    threading.Thread(target=resolve, daemon=True).start()
    try:
        records = result.get(timeout=_remaining(deadline))
    except queue.Empty:
        raise FetchDenied("notebook DNS deadline exceeded") from None
    if not records or any(not public_ip(r[4][0]) for r in records):
        raise FetchDenied("notebook destination is not exclusively public")
    return records


def _destination(url, deadline):
    if not isinstance(url, str) or len(url) > 8192 or any(ord(c) <= 32 or ord(c) == 127 for c in url):
        raise FetchDenied("invalid notebook URL")
    try:
        parsed = urlsplit(url)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None):
            raise ValueError()
        host = parsed.hostname.encode("idna").decode("ascii")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except (ValueError, UnicodeError):
        raise FetchDenied("invalid notebook URL") from None
    return parsed, host, port, _resolve(host, port, deadline)


def is_public_url(url):
    try:
        _destination(url, time.monotonic() + 5)
        return True, "ok"
    except FetchDenied:
        return False, "notebook URL is invalid, unavailable or non-public"


class _PinnedConnection(http.client.HTTPConnection):
    def __init__(self, host, port, address, deadline, tls):
        super().__init__(host, port, timeout=_remaining(deadline))
        self.address, self.deadline, self.tls = address, deadline, tls
        self.transport_socket = None

    def connect(self):
        family, kind, proto, _, sockaddr = self.address
        # sockaddr came from the one validated resolution. socket.connect with a numeric
        # sockaddr does not re-resolve the buyer-controlled hostname.
        sock = socket.socket(family, kind, proto)
        self.transport_socket = sock
        try:
            sock.settimeout(_remaining(self.deadline))
            sock.connect(sockaddr)
            if self.tls:
                # Validate certificate AND SNI against the original hostname, never the IP.
                context = ssl.create_default_context()
                sock.settimeout(_remaining(self.deadline))
                sock = context.wrap_socket(sock, server_hostname=self.host, do_handshake_on_connect=False)
                self.transport_socket = sock
                sock.do_handshake()
            self.sock = sock
        except BaseException:
            sock.close()
            raise


    def abort(self):
        sock = self.transport_socket
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        self.close()


@dataclass(frozen=True)
class Response:
    status_code: int
    content: bytes

    def raise_for_status(self):
        if not 200 <= self.status_code < 300:
            raise FetchDenied(f"notebook server returned HTTP {self.status_code}")

    def json(self):
        return json.loads(self.content)


def get(url, timeout=60, max_redirects=5, max_bytes=MAX_BYTES):
    deadline = time.monotonic() + min(float(timeout), 60.0)
    if timeout <= 0 or not 0 < max_bytes <= 128 * 1024 * 1024 or not 0 <= max_redirects <= 5:
        raise FetchDenied("invalid notebook fetch limits")
    for hop in range(max_redirects + 1):
        parsed, host, port, records = _destination(url, deadline)
        conn = _PinnedConnection(host, port, records[0], deadline, parsed.scheme == "https")
        timer = threading.Timer(_remaining(deadline), conn.abort)
        timer.daemon = True
        timer.start()
        try:
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            conn.request("GET", path, headers={"Accept-Encoding": "identity",
                         "User-Agent": "Petabyte-Notebook-Fetch/1", "Connection": "close"})
            conn.sock.settimeout(_remaining(deadline))
            response = conn.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader("Location")
                if not location or hop == max_redirects:
                    raise FetchDenied("invalid or excessive notebook redirects")
                url = urljoin(url, location)
                continue
            if response.getheader("Content-Encoding", "identity").lower() != "identity":
                raise FetchDenied("encoded notebook response refused")
            length = response.getheader("Content-Length")
            if length is not None:
                try:
                    length = int(length)
                except ValueError:
                    raise FetchDenied("invalid notebook response length") from None
                if not 0 <= length <= max_bytes:
                    raise FetchDenied("notebook response exceeds size limit")
            body = bytearray()
            while not response.isclosed():
                conn.transport_socket.settimeout(_remaining(deadline))
                block = response.read1(min(65536, max_bytes + 1 - len(body)))
                if not block:
                    break
                body.extend(block)
                if len(body) > max_bytes:
                    raise FetchDenied("notebook response exceeds size limit")
            _remaining(deadline)
            if length is not None and len(body) != length:
                raise FetchDenied("incomplete notebook response")
            return Response(response.status, bytes(body))
        except FetchDenied:
            raise
        except (OSError, http.client.HTTPException, ValueError, UnicodeError):
            # Do not return URL query strings, credentials or destination addresses in errors.
            raise FetchDenied("notebook download failed") from None
        finally:
            timer.cancel()
            conn.abort()
    raise FetchDenied("excessive notebook redirects")
