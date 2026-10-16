import contextlib
import io
import json
import socket
import ssl
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import roomba_cloud.cloud_http as probe


class DiscoveryTests(unittest.TestCase):
    def test_discovery_get_with_robot_query(self):
        payload = probe.load_discovery(
            Path(__file__).resolve().parents[1] / "roomba_cloud/discovery_response.json"
        )
        status, response = probe.http_response(
            "GET /v1/robot/discover?robot_id=test&extra_endpoints=true HTTP/1.1", payload
        )
        self.assertEqual(status, "200 OK")
        header, body = response.split(b"\r\n\r\n", 1)
        self.assertIn(f"Content-Length: {len(body)}".encode(), header)
        document = json.loads(body)
        self.assertEqual(document["httpBase"], "https://roomba.example.com")
        self.assertEqual(document["discoveryTTL"], 60)
        for key in ("mqtt", "mqttApp", "mqttAts"):
            self.assertEqual(document[key], "roomba.example.com")

    def test_other_requests_are_not_successful(self):
        for request in (
            "POST /v1/robot/discover HTTP/1.1",
            "GET /v2/register HTTP/1.1",
            "GET /v1/robot/discover/ HTTP/1.1",
        ):
            with self.subTest(request=request):
                self.assertEqual(probe.http_response(request, b"{}")[0], "501 Not Implemented")
        self.assertEqual(probe.http_response("GET / HTTP/1.1")[0], "404 Not Found")

    def test_invalid_discovery_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "response.json"
            for document in (
                [],
                {},
                {"httpBase": "http://example.com", "discoveryTTL": 60},
                {"httpBase": "https://example.com", "discoveryTTL": 0},
            ):
                path.write_text(json.dumps(document))
                with self.assertRaises(ValueError):
                    probe.load_discovery(path)

    def test_registration_is_explicit_and_route_specific(self):
        path = "/v1/" + "0" * 32 + "/register"
        request = f"POST {path} HTTP/1.1"
        self.assertEqual(probe.http_response(request, b"{}")[0], "501 Not Implemented")
        self.assertEqual(probe.http_response(request, registration_ok=True)[0], "404 Not Found")
        status, response = probe.http_response(request, b"{}", True)
        self.assertEqual(status, "200 OK")
        head, body = response.split(b"\r\n\r\n", 1)
        self.assertEqual(body, b"{}")
        self.assertIn(b"Content-Length: 2", head)
        for request in (
            f"GET {path} HTTP/1.1",
            f"POST {path}/ HTTP/1.1",
            "POST /v1/register HTTP/1.1",
            "POST /v1/not-a-robot/register HTTP/1.1",
            "POST /v1/osc/robot-registration HTTP/1.1",
            "POST /v2/" + "0" * 32 + "/register HTTP/1.1",
        ):
            with self.subTest(request=request):
                self.assertEqual(
                    probe.http_response(request, b"{}", True)[0], "501 Not Implemented"
                )
        self.assertEqual(
            probe.http_response("GET /v1/robot/discover HTTP/1.1", b'{"httpBase":"test"}', True)[
                1
            ].split(b"\r\n\r\n")[1],
            b'{"httpBase":"test"}',
        )

    def test_registration_option_requires_discovery(self):
        with (
            patch("sys.argv", ["cloud_http.py", "--registration-ok"]),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as caught,
        ):
            probe.main()
        self.assertEqual(caught.exception.code, 2)


class BodyLoggingTests(unittest.TestCase):
    def test_nested_credentials_are_redacted(self):
        document = {
            "sku": "test",
            "password": "private-password",
            "certificate": "private-certificate",
            "signature": "private-signature",
            "capabilities": {"rooms": True},
            "nested": [{"accessToken": "private-token", "private_key": "private-key"}],
        }
        with patch.object(probe, "SHOW_SECRETS", False):
            result = probe.logged_body(json.dumps(document).encode())
        self.assertNotIn("private-", result["body"])
        parsed = json.loads(result["body"])
        self.assertEqual(parsed["password"], "<redacted>")
        self.assertEqual(parsed["capabilities"], {"rooms": True})

    def test_invalid_and_scalar_bodies_are_omitted(self):
        for body in (
            b"private-password",
            b'{"password":"private',
            b"\xffprivate",
            b'"private-password"',
        ):
            with self.subTest(body=body), patch.object(probe, "SHOW_SECRETS", False):
                self.assertEqual(probe.logged_body(body)["body"], "<omitted>")
        self.assertEqual(probe.logged_body(b""), {"body": ""})

    def test_explicit_secret_logging_preserves_original(self):
        with patch.object(probe, "SHOW_SECRETS", True):
            self.assertEqual(
                probe.logged_body(b'{"password":"private"}'), {"body": '{"password":"private"}'}
            )
            self.assertEqual(probe.logged_body(b"\xff"), {"body_hex": "ff"})


class RegistrationTLSIntegrationTests(unittest.TestCase):
    def test_registration_acknowledgement_and_redacted_logging_over_tls(self):
        with tempfile.TemporaryDirectory() as directory:
            cert, key = probe.make_cert(directory, ["localhost"])
            server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_context.load_cert_chain(cert, key)
            client_context = ssl.create_default_context(cafile=str(cert))
            output = io.StringIO()
            errors = []
            with (
                socket.socket() as listener,
                contextlib.redirect_stdout(output),
                patch.object(probe, "SHOW_SECRETS", False),
            ):
                listener.bind(("127.0.0.1", 0))
                listener.listen()
                listener.settimeout(5)

                def server():
                    try:
                        raw, peer = listener.accept()
                        probe.handle(raw, peer, server_context, b"{}", True)
                    except Exception as exc:
                        errors.append(exc)

                thread = threading.Thread(target=server, daemon=True)
                thread.start()
                try:
                    with socket.create_connection(listener.getsockname(), timeout=5) as raw:
                        with client_context.wrap_socket(raw, server_hostname="localhost") as conn:
                            body = (
                                b'{"password":"private-password","signature":"private-signature"}'
                            )
                            conn.sendall(
                                (
                                    "POST /v1/" + "0" * 32 + "/register HTTP/1.1\r\n"
                                    "Host: localhost\r\nAuthorization: private-auth\r\n"
                                    f"Content-Length: {len(body)}\r\n\r\n"
                                ).encode()
                                + body
                            )
                            response = bytearray()
                            while chunk := conn.recv(8192):
                                response.extend(chunk)
                    head, payload = bytes(response).split(b"\r\n\r\n", 1)
                    self.assertTrue(head.startswith(b"HTTP/1.1 200 OK\r\n"))
                    self.assertIn(b"Content-Length: 2", head)
                    self.assertEqual(payload, b"{}")
                finally:
                    thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
            self.assertNotIn("private-", output.getvalue())
            events = [json.loads(line) for line in output.getvalue().splitlines()]
            request = next(item for item in events if item["event"] == "http_request")
            self.assertEqual(request["body_bytes"], len(body))
            self.assertEqual(json.loads(request["body"])["password"], "<redacted>")
            self.assertEqual(events[-1]["status"], "200 OK")
