import contextlib
import hashlib
import http.client
import io
import json
from pathlib import Path
import socket
import ssl
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import roomba_cloud.maps.upload as maps
import roomba_cloud.cloud_http as cloud_http
import roomba_cloud.mqtt_server as probe
from roomba_cloud.robot_mqtt import mqtt_string, packet
from roomba_cloud.cloud_http import make_cert


class MapUploadTests(unittest.TestCase):
    robot = "a" * 32

    def test_tls_failures_log_peer_and_structured_cause_without_payload(self):
        tls_error = ssl.SSLError(1, "private exception text")
        tls_error.reason = "TLSV1_ALERT_UNKNOWN_CA"
        tls_error.library = "SSL"
        for error in (
            tls_error,
            TimeoutError("private timeout text"),
            ConnectionResetError(104, "private reset text"),
        ):
            with self.subTest(error=type(error).__name__):
                server = object.__new__(maps.MapUploadServer)
                raw = Mock()
                server.socket = Mock()
                server.socket.accept.return_value = (raw, ("192.0.2.42", 12345))
                server.tls_context = Mock()
                server.tls_context.wrap_socket.side_effect = error
                with patch.object(maps, "log") as logged, self.assertRaises(type(error)):
                    server.get_request()
                raw.close.assert_called_once()
                fields = logged.call_args.kwargs
                self.assertEqual(fields["peer"], "192.0.2.42")
                self.assertEqual(fields["port"], 12345)
                self.assertEqual(fields["error_type"], type(error).__name__)
                self.assertEqual(fields["reason"], getattr(error, "reason", None))
                self.assertNotIn("private", str(logged.call_args))

    def request(self, kind="uploadP2MapMission", identifier=123):
        return json.dumps(
            {
                "xferType": kind,
                "xferId": identifier,
                kind: {
                    "missionId": "private-mission",
                    "p2maps": [{"p2map_id": "private-map", "p2mapv_id": "private-version"}],
                },
            }
        ).encode()

    @property
    def topic(self):
        return f"v011-irbthbu/things/{self.robot}/filexfer_req"

    def test_negotiation_readiness_schema_idempotence_and_privacy(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "uploads.db"
            service = maps.MapUploadService(path, "https://localhost:8443")
            with self.assertRaisesRegex(ValueError, "not reporting ready"):
                service.respond(self.topic, self.request(), self.robot)
            with patch("roomba_cloud.maps.upload.time.time", return_value=100):
                service.heartbeat()
                for kind in maps.MapUploadService.TYPES:
                    topic, result = service.respond(self.topic, self.request(kind), self.robot)
                    self.assertEqual(topic, self.topic.replace("_req", "_resp"))
                    self.assertEqual(result["status"], "success")
                    self.assertEqual(result["reqParams"], {"xferType": kind, "xferId": 123})
                    self.assertNotIn("private", json.dumps(result))
                    self.assertEqual(
                        service.respond(self.topic, self.request(kind), self.robot)[1], result
                    )
                self.assertIsNone(service.respond(self.topic, self.request(), "b" * 32))
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertIsNone(
                        service.respond(self.topic, b'{"xferType":"uploadRobotLog"}', self.robot)
                    )
                for payload in (
                    b"[]",
                    b"{",
                    self.request(identifier=True),
                    self.request(identifier=-1),
                    b'{"xferType":"uploadP2MapMission","xferId":1,"uploadP2MapMission":{"p2maps":[]}}',
                ):
                    with self.assertRaises(ValueError):
                        service.respond(self.topic, payload, self.robot)
            with patch("roomba_cloud.maps.upload.time.time", return_value=116):
                with self.assertRaisesRegex(ValueError, "not reporting ready"):
                    service.respond(self.topic, self.request(), self.robot)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("private", path.read_bytes().decode("latin1"))

    def test_private_paths_and_https_origin(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "uploads.db"
            for url in (
                "http://localhost",
                "https://user:password@localhost",
                "https://localhost/path",
                "https://localhost?token=private",
                "https://localhost:0",
            ):
                with self.assertRaises(ValueError):
                    maps.MapUploadService(path, url)
            path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "private"):
                maps.MapUploadService(path)
            path.chmod(0o600)
            link = Path(directory) / "link"
            link.symlink_to(path)
            with self.assertRaises(ValueError):
                maps.MapUploadService(link)
            Path(directory).chmod(0o755)
            with self.assertRaises(ValueError):
                maps.MapUploadService(path)

    def test_mission_only_upload_without_map_references(self):
        with tempfile.TemporaryDirectory() as directory:
            service = maps.MapUploadService(Path(directory) / "uploads.db", "https://localhost")
            service.heartbeat()
            value = {
                "xferType": "uploadP2MapMission",
                "xferId": 1792010975,
                "uploadP2MapMission": {"missionId": "private-mission", "nMssn": 122},
            }
            reply = service.respond(self.topic, json.dumps(value).encode(), self.robot)
            self.assertEqual(
                reply[1]["reqParams"], {"xferType": "uploadP2MapMission", "xferId": 1792010975}
            )
            self.assertNotIn("private", json.dumps(reply))
            for operation in (
                {"missionId": "", "nMssn": 122},
                {"missionId": "private", "nMssn": True},
                {"missionId": "private", "nMssn": -1},
                {"missionId": "private", "nMssn": 122, "p2maps": None},
                {"missionId": "private", "nMssn": 122, "p2maps": []},
            ):
                value["uploadP2MapMission"] = operation
                with self.assertRaises(ValueError):
                    service.respond(self.topic, json.dumps(value).encode(), self.robot)
            for kind in ("uploadP2MapSmart", "uploadP2MapLive"):
                value = {
                    "xferType": kind,
                    "xferId": 123,
                    kind: {"missionId": "private", "nMssn": 122},
                }
                with self.assertRaises(ValueError):
                    service.respond(self.topic, json.dumps(value).encode(), self.robot)

    def test_claim_expiry_replay_size_and_total_reservation(self):
        with tempfile.TemporaryDirectory() as directory:
            service = maps.MapUploadService(Path(directory) / "uploads.db", "https://localhost")
            with patch("roomba_cloud.maps.upload.time.time", return_value=100):
                service.heartbeat()
                token = service.respond(self.topic, self.request(), self.robot)[1]["url"].rsplit(
                    "/", 1
                )[1]
                token2 = service.respond(self.topic, self.request(identifier=124), self.robot)[1][
                    "url"
                ].rsplit("/", 1)[1]
                for length in (0, 11):
                    with self.assertRaises(maps.UploadRejected) as exc:
                        service.claim(token, length, 10, 10)
                    self.assertEqual(exc.exception.status, 413)
                service.claim(token, 8, 10, 10)
                with self.assertRaises(maps.UploadRejected) as exc:
                    service.claim(token, 8, 10, 10)
                self.assertEqual(exc.exception.status, 409)
                with self.assertRaises(maps.UploadRejected) as exc:
                    service.claim(token2, 8, 10, 10)
                self.assertEqual(exc.exception.status, 413)
            with patch("roomba_cloud.maps.upload.time.time", return_value=1001):
                with self.assertRaises(maps.UploadRejected) as exc:
                    service.claim(token2, 8, 10, 10)
                self.assertEqual(exc.exception.status, 410)

    def test_mqtt_negotiation_requires_subscription_and_sends_verified_shape(self):
        for subscribed, delay in ((False, 0), (True, 0), (True, 500)):
            with (
                self.subTest(subscribed=subscribed, delay=delay),
                tempfile.TemporaryDirectory() as directory,
            ):
                service = maps.MapUploadService(
                    Path(directory) / "uploads.db", "https://localhost:8443"
                )
                service.heartbeat()
                server_conn, client = socket.socketpair()
                errors = []
                output = io.StringIO()

                def run(server_conn=server_conn, service=service, delay=delay, errors=errors):
                    try:
                        with server_conn:
                            probe.observe(
                                server_conn,
                                "127.0.0.1",
                                {
                                    "client_id": self.robot,
                                    "keepalive_seconds": 30,
                                    "protocol_version": 5,
                                },
                                bytearray(),
                                5,
                                map_uploads=service,
                                map_response_delay_ms=delay,
                            )
                    except Exception as exc:
                        errors.append(exc)

                with client, contextlib.redirect_stdout(output):
                    thread = threading.Thread(target=run)
                    thread.start()
                    try:
                        buffer = bytearray()
                        if subscribed:
                            subscription = (
                                b"\0\x01\0"
                                + mqtt_string(self.topic.replace("_req", "_resp"))
                                + b"\0"
                            )
                            client.sendall(packet(0x82, subscription))
                            self.assertEqual(probe.read_frame(client, 5, buffer)[0], 0x90)
                        client.sendall(
                            packet(0x32, mqtt_string(self.topic) + b"\0\x02\0" + self.request())
                        )
                        self.assertEqual(probe.read_frame(client, 5, buffer), (0x40, b"\0\x02"))
                        if subscribed:
                            acknowledged_at = time.monotonic()
                            if delay:
                                client.settimeout(0.1)
                                with self.assertRaises(TimeoutError):
                                    client.recv(1)
                            header, body = probe.read_frame(client, 5, buffer)
                            if delay:
                                self.assertGreaterEqual(time.monotonic() - acknowledged_at, 0.4)
                            self.assertEqual(header, 0x30)
                            reader = probe.PacketReader(body)
                            self.assertEqual(reader.text(), self.topic.replace("_req", "_resp"))
                            reader.properties()
                            result = json.loads(reader.take(len(body) - reader.pos))
                            self.assertEqual(result["reqParams"]["xferId"], 123)
                            self.assertTrue(
                                result["url"].startswith("https://localhost:8443/map-upload/")
                            )
                        client.sendall(packet(0xC0))
                        self.assertEqual(probe.read_frame(client, 5, buffer), (0xD0, b""))
                        client.sendall(packet(0xE0))
                    finally:
                        thread.join(6)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
                self.assertNotIn("private", output.getvalue())
                self.assertNotIn("/map-upload/", output.getvalue())
                with service.connect() as db:
                    self.assertEqual(
                        db.execute("SELECT count(*) FROM uploads").fetchone()[0], int(subscribed)
                    )

    def test_https_put_stores_exact_private_file_before_200_and_rejects_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            cert, key = make_cert(directory, ["localhost"])
            service = maps.MapUploadService(Path(directory) / "uploads.db", "https://localhost")
            server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_context.load_cert_chain(cert, key)
            payload = b"\x1f\x8b\0private-map-binary\xff"
            output = io.StringIO()
            with (
                cloud_http.cloud_server()(("127.0.0.1", 0), cloud_http.cloud_handler()) as server,
                contextlib.redirect_stdout(output),
            ):
                server.customer_auth = cloud_http.CustomerAuth(b"a" * 64)
                server.uploads = service
                server.max_bytes = 1024
                server.max_total = 2048
                server.tls_context = server_context
                server.discovery = b'{"httpBase":"https://localhost"}'
                server.registration_ok = True
                service.heartbeat()
                url = service.respond(self.topic, self.request(), self.robot)[1]["url"]
                path = "/map-upload/" + url.rsplit("/", 1)[1]
                thread = threading.Thread(
                    target=server.serve_forever, kwargs={"poll_interval": 0.05}
                )
                thread.start()
                context = ssl.create_default_context(cafile=str(cert))

                def put(request_path, body, headers=None):
                    conn = http.client.HTTPSConnection(
                        "localhost", server.server_port, context=context, timeout=5
                    )
                    try:
                        conn.request("PUT", request_path, body=body, headers=headers or {})
                        response = conn.getresponse()
                        status = response.status
                        response.read()
                        return status
                    finally:
                        conn.close()

                try:
                    for method, route, body, expected_status, expected_body in (
                        ("GET", "/v1/robot/discover?robot_id=test", None, 200, server.discovery),
                        (
                            "POST",
                            "/v1/" + self.robot + "/register",
                            b'{"password":"private-password"}',
                            200,
                            b"{}",
                        ),
                        ("GET", "/unknown", None, 501, b'{"message":"endpoint not implemented"}'),
                        ("GET", path, None, 405, b""),
                    ):
                        conn = http.client.HTTPSConnection(
                            "localhost", server.server_port, context=context, timeout=5
                        )
                        try:
                            headers = (
                                {"Authorization": "Bearer " + "a" * 64}
                                if route == "/unknown"
                                else {}
                            )
                            conn.request(method, route, body=body, headers=headers)
                            response = conn.getresponse()
                            self.assertEqual(response.status, expected_status)
                            self.assertEqual(response.read(), expected_body)
                        finally:
                            conn.close()
                    self.assertEqual(put(path, payload), 200)
                    token = path.rsplit("/", 1)[1]
                    target = Path(directory) / (token + ".tar.gz")
                    self.assertEqual(target.read_bytes(), payload)
                    self.assertEqual(target.stat().st_mode & 0o777, 0o600)
                    with service.connect() as db:
                        self.assertEqual(
                            db.execute(
                                "SELECT status,bytes,sha256 FROM uploads WHERE token=?", (token,)
                            ).fetchone(),
                            ("complete", len(payload), hashlib.sha256(payload).hexdigest()),
                        )
                    self.assertEqual(put(path, payload), 409)
                    self.assertEqual(put("/map-upload/" + "0" * 64, payload), 404)
                    self.assertEqual(put("/map-upload/../../private", payload), 404)
                    path2 = service.respond(self.topic, self.request(identifier=124), self.robot)[
                        1
                    ]["url"]
                    self.assertEqual(
                        put("/map-upload/" + path2.rsplit("/", 1)[1], b"x" * 1025), 413
                    )
                    path3 = (
                        "/map-upload/"
                        + service.respond(self.topic, self.request(identifier=125), self.robot)[1][
                            "url"
                        ].rsplit("/", 1)[1]
                    )
                    conn = http.client.HTTPSConnection(
                        "localhost", server.server_port, context=context, timeout=5
                    )
                    try:
                        conn.request("PUT", path3, body=b"short", headers={"Content-Length": "10"})
                        conn.sock.shutdown(socket.SHUT_WR)
                        conn.close()
                    finally:
                        conn.close()
                    deadline = time.monotonic() + 5
                    token3 = path3.rsplit("/", 1)[1]
                    while time.monotonic() < deadline:
                        with service.connect() as db:
                            state = db.execute(
                                "SELECT status FROM uploads WHERE token=?", (token3,)
                            ).fetchone()[0]
                        if state == "failed":
                            break
                        time.sleep(0.01)
                    self.assertEqual(state, "failed")
                    self.assertFalse((Path(directory) / (token3 + ".tar.gz")).exists())
                    self.assertEqual(put(path3, payload), 409)
                finally:
                    server.shutdown()
                    thread.join(5)
                self.assertFalse(thread.is_alive())
            self.assertNotIn("private-map", output.getvalue())
            self.assertNotIn("private-password", output.getvalue())
            self.assertNotIn(path, output.getvalue())


if __name__ == "__main__":
    unittest.main()
