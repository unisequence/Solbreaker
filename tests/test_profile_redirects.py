"""Every redirect hop preserves HTTPS before the next request is sent."""
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import ssl
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from solbreaker.management_tasks import fetch_profile


class ProfileRedirectTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')])
        now = datetime.now(timezone.utc)
        certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now-timedelta(minutes=1)).not_valid_after(now+timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost')]), critical=False)
            .sign(key, hashes.SHA256()))
        cert_path, key_path = root/'cert.pem', root/'key.pem'
        cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        trusted = patch.dict(os.environ, SSL_CERT_FILE=str(cert_path))
        trusted.start()
        self.addCleanup(trusted.stop)
        self.events = []
        self.mode = 'ok'
        self.payload = b'{"server_name":"Synthetic test server"}'
        test = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                secure = isinstance(self.connection, ssl.SSLSocket)
                test.events.append(('https' if secure else 'http', self.path))
                location = None
                if self.path == '/bounce':
                    location = test.https+'/final'
                elif secure and self.path == '/tfs/connection/v1':
                    location = {'chain': test.http+'/bounce', 'direct': test.http+'/final',
                                'secure': test.https+'/final'}.get(test.mode)
                elif not secure and self.path == '/tfs/connection/v1' and test.mode == 'upgrade-downgrade':
                    location = test.https+'/tfs/connection/v1'
                if secure and test.mode == 'upgrade-downgrade':
                    location = test.http+'/final'
                if location:
                    self.send_response(302)
                    self.send_header('Location', location)
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(test.payload)))
                    self.end_headers()
                    self.wfile.write(test.payload)

            def log_message(self, *args):
                pass

        self.http_server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.https_server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert_path, key_path)
        self.https_server.socket = context.wrap_socket(self.https_server.socket, server_side=True)
        self.http = f'http://127.0.0.1:{self.http_server.server_port}'
        self.https = f'https://localhost:{self.https_server.server_port}'
        for server in (self.http_server, self.https_server):
            thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
            thread.start()
            self.addCleanup(self.stop_server, server, thread)

    @staticmethod
    def stop_server(server, thread):
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    def test_https_http_https_chain_is_rejected_before_any_plaintext_request(self):
        self.mode = 'chain'
        with self.assertRaisesRegex(ValueError, 'HTTPS'):
            fetch_profile(self.https)
        self.assertEqual(self.events, [('https', '/tfs/connection/v1')])

    def test_direct_downgrade_is_rejected_before_any_plaintext_request(self):
        self.mode = 'direct'
        with self.assertRaisesRegex(ValueError, 'HTTPS'):
            fetch_profile(self.https)
        self.assertEqual(self.events, [('https', '/tfs/connection/v1')])

    def test_secure_redirect_remains_supported(self):
        self.mode = 'secure'
        self.assertEqual(fetch_profile(self.https), self.payload)
        self.assertEqual(self.events, [('https', '/tfs/connection/v1'), ('https', '/final')])

    def test_explicit_local_http_and_direct_https_remain_supported(self):
        self.assertEqual(fetch_profile(self.http), self.payload)
        self.assertEqual(fetch_profile(self.https), self.payload)

    def test_http_upgrade_does_not_allow_a_subsequent_downgrade(self):
        self.mode = 'upgrade-downgrade'
        with self.assertRaisesRegex(ValueError, 'HTTPS'):
            fetch_profile(self.http)
        self.assertEqual(self.events, [('http', '/tfs/connection/v1'), ('https', '/tfs/connection/v1')])
