import contextlib
import http.client
import io
import json
from pathlib import Path
import ssl
import tempfile
import threading
import unittest
from unittest.mock import patch

import roomba_cloud.cloud_http as cloud


class CustomerAuthTests(unittest.TestCase):
    token = "aB0_" * 16

    def test_private_token_file_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token"
            path.write_text(self.token + "\n")
            path.chmod(0o600)
            auth = cloud.CustomerAuth.load(path)
            self.assertTrue(auth.accepts(["Bearer " + self.token]))
            self.assertTrue(auth.accepts(["bearer " + self.token]))
            for values in (
                [],
                ["Bearer wrong"],
                ["Basic " + self.token],
                ["Bearer " + self.token, "Bearer " + self.token],
                ["Bearer " + self.token + " "],
                ["Bearer " + "x" * 64],
            ):
                self.assertFalse(auth.accepts(values))
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                cloud.CustomerAuth.load(path)
            path.chmod(0o600)
            for value in ("", "short", "a" * 129, "a" * 128 + "\n" * 4):
                path.write_text(value)
                with self.assertRaises(ValueError):
                    cloud.CustomerAuth.load(path)
            link = Path(directory) / "link"
            link.symlink_to(path)
            with self.assertRaises(OSError):
                cloud.CustomerAuth.load(link)

    def test_cli_fails_closed_for_missing_token_and_required_settings(self):
        required = ["--cert", "/test/cert", "--key", "/test/key"]
        for args in (
            ["--customer-token-file", "/nonexistent/customer-token"],
            ["--customer-token-file"],
        ):
            with (
                patch("sys.argv", ["cloud_http.py", *required, *args]),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as caught,
            ):
                cloud.main()
            self.assertEqual(caught.exception.code, 2)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token"
            path.write_text(self.token)
            path.chmod(0o600)
            with (
                patch("sys.argv", ["cloud_http.py", "--customer-token-file", str(path)]),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as caught,
            ):
                cloud.main()
            self.assertEqual(caught.exception.code, 2)

    def test_customer_tls_auth_and_robot_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            cert, key = cloud.make_cert(directory, ["localhost"])
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls.load_cert_chain(cert, key)
            client_tls = ssl.create_default_context(cafile=str(cert))
            output = io.StringIO()
            with (
                cloud.cloud_server()(("127.0.0.1", 0), cloud.cloud_handler()) as server,
                contextlib.redirect_stdout(output),
                patch.object(cloud, "SHOW_SECRETS", True),
            ):
                server.customer_auth = cloud.CustomerAuth(self.token.encode())
                server.uploads = None
                server.tls_context = tls
                server.discovery = b"{}"
                server.registration_ok = True
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()

                def request(method, path, auth=None, source="127.0.0.1"):
                    conn = http.client.HTTPSConnection(
                        "localhost",
                        server.server_port,
                        context=client_tls,
                        timeout=5,
                        source_address=(source, 0),
                    )
                    try:
                        conn.putrequest(method, path)
                        if auth is not None:
                            for value in auth:
                                conn.putheader("Authorization", value)
                        conn.endheaders()
                        response = conn.getresponse()
                        return response.status, dict(response.getheaders()), response.read()
                    finally:
                        conn.close()

                try:
                    for values in (
                        None,
                        ["Basic " + self.token],
                        ["Bearer " + "x" * 64],
                        ["Bearer " + self.token] * 2,
                    ):
                        status, headers, _ = request("GET", "/v1/customer/session", values)
                        self.assertEqual(status, 401)
                        self.assertIn("WWW-Authenticate", headers)
                    authorized = ["Bearer " + self.token]
                    status, headers, body = request("GET", "/v1/customer/session", authorized)
                    self.assertEqual(status, 200)
                    self.assertEqual(json.loads(body), {"authenticated": True})
                    self.assertEqual(headers["Cache-Control"], "no-store")
                    self.assertEqual(request("POST", "/v1/customer/session", authorized)[0], 405)
                    for method in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
                        self.assertEqual(request(method, "/v1/p2maps")[0], 401)
                    self.assertEqual(
                        request("GET", "/v1/p2maps?token=" + self.token, authorized)[0], 501
                    )
                    locate = "/v1/customer/robots/" + "a" * 32 + "/locate"
                    self.assertEqual(request("POST", locate)[0], 401)
                    self.assertEqual(request("POST", locate, authorized)[0], 503)
                    self.assertEqual(request("GET", locate, authorized)[0], 405)
                    server.command_socket = "/test-only/socket"
                    with patch(
                        "roomba_cloud.robot_commands.request_command",
                        return_value={"delivery": "acknowledged", "execution": "unconfirmed"},
                    ) as delivery:
                        status, _, body = request("POST", locate, authorized)
                        self.assertEqual(status, 200)
                        self.assertEqual(json.loads(body)["execution"], "unconfirmed")
                        delivery.assert_called_once_with("/test-only/socket", "a" * 32)
                    with patch(
                        "roomba_cloud.robot_commands.request_command",
                        side_effect=OSError("disconnected"),
                    ):
                        self.assertEqual(request("POST", locate, authorized)[0], 503)
                    for command in ("start", "pause", "stop", "dock"):
                        route = "/v1/customer/robots/" + "a" * 32 + "/commands/" + command
                        self.assertEqual(request("POST", route)[0], 401)
                        self.assertEqual(request("GET", route, authorized)[0], 405)
                        with patch(
                            "roomba_cloud.robot_commands.request_command",
                            return_value={"delivery": "acknowledged", "execution": "unconfirmed"},
                        ) as delivery:
                            self.assertEqual(request("POST", route, authorized)[0], 200)
                            delivery.assert_called_once_with("/test-only/socket", "a" * 32, command)
                    self.assertEqual(request("GET", "/v1/robot/discover", authorized)[0], 200)
                    self.assertEqual(
                        request("GET", "/v1/robot/discover", source="127.0.0.2")[0], 200
                    )
                    self.assertEqual(
                        request("POST", "/v1/" + "a" * 32 + "/register", source="127.0.0.2")[0], 200
                    )
                    self.assertEqual(request("PUT", "/map-upload/" + "b" * 64, authorized)[0], 404)
                    server.customer_auth = None
                    self.assertEqual(
                        request("GET", "/v1/customer/session", source="127.0.0.2")[0], 503
                    )
                finally:
                    server.shutdown()
                    thread.join(5)
                self.assertFalse(thread.is_alive())
            self.assertNotIn(self.token, output.getvalue())


if __name__ == "__main__":
    unittest.main()
