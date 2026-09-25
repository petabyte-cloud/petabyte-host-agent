"""CPU-only real-socket regressions for host notebook fetching. No external network."""
import json
import os
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
import safe_fetch as sf


class Server(ThreadingHTTPServer):
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/redirect-private":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1/private")
            self.end_headers()
        elif self.path == "/oversize":
            self.send_response(200)
            self.send_header("Content-Length", str(sf.MAX_BYTES + 1))
            self.end_headers()
        elif self.path == "/chunked-large":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"x" * 1025)
        elif self.path == "/slow":
            self.send_response(200)
            self.end_headers()
            try:
                for _ in range(30):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(.05)
            except OSError:
                pass
        elif self.path == "/gzip":
            self.send_response(200)
            self.send_header("Content-Encoding", "gzip")
            self.end_headers()
        else:
            self.send_response(200)
            self.send_header("Content-Length", "11")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')


class FetchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = Server(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def fetch(self, path="/", scheme="http", **kwargs):
        real_connect = socket.socket.connect
        resolutions, destinations = [], []
        port = self.server.server_address[1]

        def resolve(*args, **kw):
            resolutions.append(args[0])
            # An attacker changes DNS after validation; a second resolution must never occur.
            ip = "1.1.1.1" if len(resolutions) == 1 else "127.0.0.1"
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port))]

        def route(sock, address):
            destinations.append(address)
            self.assertEqual(address, ("1.1.1.1", port))
            # Emulate routing the public fixture address to a local server; HTTP and socket
            # reads are real. No connection can leave the test host.
            return real_connect(sock, ("127.0.0.1", port))

        with patch("socket.getaddrinfo", side_effect=resolve), patch.object(socket.socket, "connect", route):
            result = sf.get(f"{scheme}://notebook.example.test:{port}{path}", **kwargs)
        self.assertEqual(len(resolutions), 1)
        self.assertEqual(len(destinations), 1)
        return result

    def test_real_http_pinned_despite_rebinding(self):
        self.assertEqual(self.fetch().json(), {"ok": True})

    def test_tls_uses_original_hostname_and_rejects_wrong_certificate(self):
        import ssl
        import tempfile
        from datetime import datetime, timedelta, timezone
        from pathlib import Path
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        for cert_host, allowed in (("notebook.example.test", True), ("wrong.example.test", False)):
            with self.subTest(cert_host=cert_host), tempfile.TemporaryDirectory() as tmp:
                key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
                name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cert_host)])
                now = datetime.now(timezone.utc)
                cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                        .public_key(key.public_key()).serial_number(x509.random_serial_number())
                        .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(hours=1))
                        .add_extension(x509.SubjectAlternativeName([x509.DNSName(cert_host)]), critical=False)
                        .sign(key, hashes.SHA256()))
                cert_path, key_path = Path(tmp)/"cert.pem", Path(tmp)/"key.pem"
                cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
                key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                ctx.load_cert_chain(cert_path, key_path)
                sni = []
                ctx.set_servername_callback(lambda sock, servername, context: sni.append(servername))
                server = Server(("127.0.0.1", 0), Handler)
                server.socket = ctx.wrap_socket(server.socket, server_side=True)
                thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
                old = self.server; self.server = server
                try:
                    with patch.dict(os.environ, {"SSL_CERT_FILE": str(cert_path)}):
                        if allowed:
                            self.assertEqual(self.fetch(scheme="https").json(), {"ok": True})
                        else:
                            with self.assertRaises(sf.FetchDenied):
                                self.fetch(scheme="https")
                    self.assertEqual(sni, ["notebook.example.test"])
                finally:
                    self.server = old; server.shutdown(); server.server_close(); thread.join()

    def test_environment_proxy_is_ignored(self):
        with patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:1", "ALL_PROXY": "http://127.0.0.1:1"}):
            self.assertEqual(self.fetch().status_code, 200)

    def test_redirect_private_is_rejected(self):
        with self.assertRaises(sf.FetchDenied):
            self.fetch("/redirect-private")

    def test_bounded_length(self):
        with self.assertRaises(sf.FetchDenied):
            self.fetch("/oversize")

    def test_bounded_actual_bytes_without_length(self):
        with self.assertRaises(sf.FetchDenied):
            self.fetch("/chunked-large", max_bytes=1024)

    def test_no_decompression(self):
        with self.assertRaises(sf.FetchDenied):
            self.fetch("/gzip")

    def test_trickled_body_has_absolute_deadline(self):
        start = time.monotonic()
        with self.assertRaises(sf.FetchDenied):
            self.fetch("/slow", timeout=.15)
        self.assertLess(time.monotonic() - start, 1)

    def test_nonpublic_and_transition_addresses(self):
        for ip in ("127.0.0.1", "169.254.169.254", "10.0.0.1", "172.20.1.1",
                   "192.168.1.1", "100.64.0.1", "::1", "fe80::1", "fc00::1",
                   "::ffff:127.0.0.1", "2002:7f00:1::", "64:ff9b::7f00:1",
                   "224.0.0.1", "0.0.0.0", "fe80::1%eth0"):
            with self.subTest(ip=ip):
                self.assertFalse(sf.public_ip(ip))

    def test_mixed_public_private_dns_rejected(self):
        entries = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 80))
                   for ip in ("1.1.1.1", "127.0.0.1")]
        with patch("socket.getaddrinfo", return_value=entries), patch("socket.socket.connect") as connect:
            with self.assertRaises(sf.FetchDenied):
                sf.get("http://notebook.example.test")
            connect.assert_not_called()

    def test_credentials_and_schemes_rejected(self):
        for url in ("http://user:password@example.test", "file:///etc/passwd",
                    "http://example.test/\r\nHeader:value", "http://[::1]", "http://2130706433"):
            with self.subTest(url=url):
                self.assertFalse(sf.is_public_url(url)[0])


if __name__ == "__main__":
    unittest.main()
