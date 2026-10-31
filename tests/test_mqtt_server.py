import contextlib
import base64
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import ssl
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import roomba_cloud.mqtt_server as probe
from roomba_cloud.robot_mqtt import ProtocolError, mqtt_string, packet
from roomba_cloud.cloud_http import make_cert


class SessionMaintenanceTests(unittest.TestCase):
    def test_blocked_storage_does_not_delay_ping_publish_or_command_ack(self):
        entered, release, acknowledged = threading.Event(), threading.Event(), threading.Event()
        shadows = Mock()
        shadows.respond.return_value = None
        shadows.touch_session.side_effect = lambda *a, **kw: (entered.set(), release.wait(5))
        maintenance = probe.SessionMaintenance(shadows)
        maintenance.interval = 0.01
        maintenance.join_timeout = 0.02
        server, client = socket.socketpair()
        with server, client, contextlib.redirect_stdout(io.StringIO()), maintenance:
            client.settimeout(1)
            command = Mock(wraps=server)
            command.subscriptions = set()
            command.acknowledge = Mock(side_effect=lambda *a: acknowledged.set())
            reader = threading.Thread(
                target=probe.observe,
                args=(
                    server,
                    "local",
                    {"keepalive_seconds": 30, "protocol_version": 4, "client_id": "robot"},
                    bytearray(),
                    None,
                ),
                kwargs={
                    "shadows": shadows,
                    "session": "old",
                    "maintenance": maintenance,
                    "command_session": command,
                },
            )
            reader.start()
            try:
                client.sendall(packet(0xC0, b""))
                self.assertEqual(probe.read_frame(client, 1, bytearray()), (0xD0, b""))
                self.assertTrue(entered.wait(1))
                client.sendall(packet(0xC0, b""))
                self.assertEqual(probe.read_frame(client, 1, bytearray()), (0xD0, b""))
                client.sendall(packet(0x32, mqtt_string("unrelated") + b"\0\x01{}"))
                self.assertEqual(probe.read_frame(client, 1, bytearray()), (0x40, b"\0\x01"))
                client.sendall(packet(0x40, b"\0\x02"))
                self.assertTrue(acknowledged.wait(1))
                client.sendall(packet(0xE0, b""))
                reader.join(1)
                self.assertFalse(reader.is_alive())
                self.assertEqual(shadows.touch_session.call_count, 1)
                maintenance.end("old")
                maintenance.__exit__()
                self.assertTrue(maintenance.worker.is_alive())
            finally:
                release.set()
                client.shutdown(socket.SHUT_RDWR)
                reader.join(2)
                maintenance.worker.join(2)
        shadows.end_session.assert_called_once_with("old")
        self.assertEqual(shadows.touch_session.call_count, 1)

    def test_coalesces_activity_and_preserves_publication_timestamp(self):
        shadows = Mock()
        maintenance = probe.SessionMaintenance(shadows)
        with patch.object(probe.time, "time", side_effect=[100, 101, 102]):
            maintenance.record("session", 65, True)
            maintenance.record("session", 65)
            maintenance.record("session", 65)
        maintenance._flush()
        shadows.touch_session.assert_called_once_with(
            "session", 65, observed_at=102, data_received_at=100
        )
        maintenance._flush()
        self.assertEqual(shadows.touch_session.call_count, 1)

    def test_failed_touch_retries_without_losing_newer_data(self):
        shadows = Mock()
        maintenance = probe.SessionMaintenance(shadows)

        def fail(*args, **kwargs):
            with patch.object(probe.time, "time", return_value=102):
                maintenance.record("session", 65)
            raise sqlite3.OperationalError("busy")

        with patch.object(probe.time, "time", return_value=100):
            maintenance.record("session", 65, True)
        shadows.touch_session.side_effect = fail
        with contextlib.redirect_stdout(io.StringIO()) as output:
            maintenance._flush()
        self.assertIn("mqtt_session_maintenance_failed", output.getvalue())
        shadows.touch_session.side_effect = None
        maintenance._flush()
        self.assertEqual(
            shadows.touch_session.call_args.kwargs, {"observed_at": 102, "data_received_at": 100}
        )

    def test_failed_cleanup_retries_and_does_not_renew_disconnected_session(self):
        shadows = Mock()
        maintenance = probe.SessionMaintenance(shadows)
        maintenance.record("old", 65, True)
        maintenance.end("old")
        shadows.end_session.side_effect = sqlite3.OperationalError("busy")
        with contextlib.redirect_stdout(io.StringIO()) as output:
            maintenance._flush()
        self.assertIn('"operation": "end"', output.getvalue())
        shadows.end_session.side_effect = None
        maintenance._flush()
        shadows.touch_session.assert_not_called()
        self.assertEqual(shadows.end_session.call_count, 2)
        self.assertEqual(maintenance.ended, set())

    def test_deferred_timestamps_and_old_session_cleanup_preserve_new_session(self):
        from roomba_cloud.shadow_service import ShadowService

        with tempfile.TemporaryDirectory() as directory:
            service = ShadowService(Path(directory) / "state.db")
            with patch("roomba_cloud.shadow_service.time.time", return_value=50):
                old = service.start_session("robot", 65, "127.0.0.1")
                new = service.start_session("robot", 65, "127.0.0.1")
            service.touch_session(new, 65, observed_at=200, data_received_at=190)
            service.touch_session(old, 65, observed_at=100, data_received_at=90)
            service.end_session(old)
            service.touch_session(old, 65, observed_at=300, data_received_at=300)
            with contextlib.closing(sqlite3.connect(service.path)) as db:
                self.assertEqual(
                    db.execute("SELECT session, expires FROM mqtt_sessions").fetchall(),
                    [(new, 265)],
                )
                self.assertEqual(
                    db.execute("SELECT received_at FROM robot_data").fetchone(), (190,)
                )
                self.assertEqual(
                    db.execute("SELECT last_seen FROM robot_connections").fetchone(), (200,)
                )


class PrivateCaptureTests(unittest.TestCase):
    def fields(self, topic, size=0):
        return {
            "topic": topic,
            "qos": 1,
            "packet_id": 1,
            "payload_bytes": size,
            "retain": False,
            "duplicate": False,
        }

    def test_permissions_exact_bytes_empty_payload_and_topic_allowlist(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private.jsonl"
            with probe.PrivateCapture(path) as capture:
                for suffix, payload in (
                    ("ro-currentstate/update", b'{"state":{"reported":{"batPct":72}}}'),
                    ("rw-software/get", b""),
                    ("na-irbtfeatures/get", b"\xff\0"),
                ):
                    topic = "$aws/things/test/shadow/name/" + suffix
                    self.assertTrue(
                        capture.record(
                            "127.0.0.1", "test", self.fields(topic, len(payload)), payload
                        )
                    )
                for topic in (
                    "v011-irbthbu/things/test/matter/fabric/req",
                    "$aws/things/other/shadow/name/ro-currentstate/update",
                    "v011-irbthbu/things/test/cmd",
                ):
                    self.assertFalse(
                        capture.record("127.0.0.1", "test", self.fields(topic), b"private-excluded")
                    )
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            records = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(len(records), 3)
            self.assertEqual(base64.b64decode(records[1]["payload_base64"]), b"")
            self.assertEqual(base64.b64decode(records[2]["payload_base64"]), b"\xff\0")
            self.assertNotIn("private-excluded", path.read_text())

    def test_exclusive_file_and_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "existing"
            target.write_text("untouched")
            link = Path(directory) / "link"
            link.symlink_to(target)
            for path in (target, link):
                with self.assertRaises(FileExistsError):
                    probe.PrivateCapture(path)
            self.assertEqual(target.read_text(), "untouched")

    def test_expanded_capture_families_preserve_privacy_boundaries(self):
        custom = "v011-irbthbu/things/test/"
        aws = "$aws/things/test/shadow/"
        allowed = [aws + action for action in ("get", "update")]
        allowed += [
            aws + "name/" + name + "/" + action
            for name in (
                "ro-currentstate",
                "na-irbtfeatures",
                "ro-configinfo",
                "ro-services",
                "ro-stats",
                "rw-constatus",
                "rw-schedule",
                "rw-settings",
                "rw-software",
            )
            for action in ("get", "update")
        ]
        allowed += [
            custom + suffix
            for suffix in (
                "livemap",
                "livemap/data",
                "livemap/data/part",
                "mission/timeline",
                "mission/timeline/events",
                "mapdetails/req",
                "filexfer_req",
            )
        ]
        excluded = [
            custom + suffix
            for suffix in (
                "livemap/",
                "livemap/cmd",
                "livemap/cmd/start",
                "livemap/data/cmd",
                "mission/timeline/",
                "mission/timeline/request",
                "mission/timeline/request/start",
                "matter/fabric/req",
                "matter/certificate/req",
                "cmd",
                "deployment",
                "edit_req",
                "livemaps/data",
                "mission/timelines/events",
            )
        ]
        excluded += [
            "v011-irbthbu/things/other/livemap/data",
            "v011-irbthbu/things/test-extra/mission/timeline/events",
            aws + "name/unknown/update",
            aws + "name/rw-settings/update/delta",
            "$aws/things/other/shadow/name/rw-settings/update",
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private.jsonl"
            with probe.PrivateCapture(path) as capture:
                for topic in allowed:
                    with self.subTest(topic=topic):
                        self.assertTrue(
                            capture.record(
                                "127.0.0.1", "test", self.fields(topic, 3), b"\0\xff\x01"
                            )
                        )
                for topic in excluded:
                    with self.subTest(topic=topic):
                        self.assertFalse(
                            capture.record(
                                "127.0.0.1", "test", self.fields(topic), b"private-excluded"
                            )
                        )
            records = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual([r["topic"] for r in records], allowed)
            self.assertTrue(
                all(base64.b64decode(r["payload_base64"]) == b"\0\xff\x01" for r in records)
            )
            self.assertNotIn("private-excluded", path.read_text())

    def test_public_directory_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            os.chmod(directory, 0o755)
            with self.assertRaisesRegex(ValueError, "group/other"):
                probe.PrivateCapture(Path(directory) / "capture")

    def test_size_limit_is_explicit_and_never_exceeded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture"
            fields = self.fields("v011-irbthbu/things/test/filexfer_req", 6)
            with probe.PrivateCapture(path) as capture:
                capture.record("127.0.0.1", "test", fields, b"secret")
                capture.max_bytes = capture.bytes_written
                with self.assertRaisesRegex(ProtocolError, "size limit"):
                    capture.record("127.0.0.1", "test", fields, b"secret")
                self.assertEqual(path.stat().st_size, capture.max_bytes)
                with self.assertRaisesRegex(ProtocolError, "unavailable"):
                    capture.record("127.0.0.1", "test", fields, b"secret")

    def test_capture_io_error_is_not_hidden(self):
        with tempfile.TemporaryDirectory() as directory:
            with probe.PrivateCapture(Path(directory) / "capture") as capture:
                with patch.object(probe.os, "fsync", side_effect=OSError("test disk error")):
                    with self.assertRaisesRegex(OSError, "disk error"):
                        capture.record(
                            "127.0.0.1",
                            "test",
                            self.fields("v011-irbthbu/things/test/mapdetails/req"),
                            b"",
                        )
                self.assertTrue(capture.failed)


def connect_body(version=4, flags=0xC2):
    body = mqtt_string("MQIsdp" if version == 3 else "MQTT")
    body += bytes([version, flags]) + b"\0\x1e"
    if version == 5:
        body += b"\0"
    body += mqtt_string("test-robot")
    if flags & 0x80:
        body += mqtt_string("private-username")
    if flags & 0x40:
        body += mqtt_string("private-password")
    return body


class ConnectTests(unittest.TestCase):
    def test_observation_responses_and_private_properties(self):
        self.assertEqual(probe.successful_connack(5), b"\x20\x0a\0\0\x07\x24\x01\x25\0\x22\0\0")
        self.assertEqual(probe.successful_connack(4), b"\x20\x02\0\0")
        for version in (3, 4, 5):
            properties = b"\0" if version == 5 else b""
            event, fields, response, done = probe.observation_packet(
                0x82, b"\0\x01" + properties + mqtt_string("state/#") + b"\x02", version
            )
            self.assertEqual(event, "mqtt_subscribe")
            self.assertEqual(fields["topics"][0]["granted_qos"], 1)
            self.assertEqual(response, packet(0x90, b"\0\x01" + properties + b"\x01"))
            self.assertFalse(done)
            _, _, response, _ = probe.observation_packet(
                0xA2, b"\0\x02" + properties + mqtt_string("state/#"), version
            )
            self.assertEqual(response, packet(0xB0, b"\0\x02" + (b"\0\0" if version == 5 else b"")))
        property_body = b"\x26" + mqtt_string("secret") + mqtt_string("private-value")
        prop = bytes([len(property_body)]) + property_body
        _, fields, response, _ = probe.observation_packet(
            0x32, mqtt_string("state") + b"\0\x03" + prop + b"private-payload", 5
        )
        self.assertEqual(fields["payload_bytes"], 15)
        self.assertNotIn("private", json.dumps(fields))
        self.assertEqual(response, b"\x40\x02\0\x03")
        self.assertEqual(probe.observation_packet(0xC0, b"", 5)[2], b"\xd0\0")
        self.assertTrue(probe.observation_packet(0xE0, b"", 5)[3])

    def test_malformed_observation_packets(self):
        for header, body in (
            (0x82, b"\0\0\0"),
            (0x82, b"\0\x01\0"),
            (0x82, b"\0\x01\0" + mqtt_string("bad/#/filter") + b"\0"),
            (0x82, b"\0\x01\0" + mqtt_string("state") + b"\xc0"),
            (0x34, mqtt_string("state")),
            (0x30, mqtt_string("state") + b"\x80"),
            (0xC0, b"x"),
            (0xE1, b""),
            (0x10, b"private-data"),
        ):
            with self.subTest(header=header, body=body):
                with self.assertRaises(ProtocolError) as caught:
                    probe.observation_packet(header, body, 5)
                self.assertNotIn("private", str(caught.exception))

    def test_observation_duration_limit(self):
        from unittest.mock import Mock

        output = io.StringIO()
        with (
            patch.object(probe.time, "monotonic", side_effect=[0, 6]),
            contextlib.redirect_stdout(output),
        ):
            conn = Mock()
            probe.observe(conn, "127.0.0.1", {"keepalive_seconds": 30}, bytearray(), 5)
            conn.recv.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["reason"], "duration limit")

    def test_versions_and_rejection(self):
        for version in (3, 4, 5):
            with self.subTest(version=version):
                result = probe.connect_metadata(0x10, connect_body(version))
                self.assertEqual(result["protocol_version"], version)
                self.assertEqual(result["client_id"], "test-robot")
                self.assertEqual(result["keepalive_seconds"], 30)
                self.assertEqual(result["username_bytes"], 16)
                self.assertEqual(result["password_bytes"], 16)
                self.assertNotIn("private", json.dumps(result))
                self.assertEqual(
                    probe.unavailable_connack(version),
                    b"\x20\x03\0\x88\0" if version == 5 else b"\x20\x02\0\x03",
                )

    def test_anonymous_and_binary_will_and_password(self):
        result = probe.connect_metadata(0x10, connect_body(flags=2))
        self.assertFalse(result["username_present"])
        self.assertFalse(result["password_present"])
        body = (
            mqtt_string("MQTT")
            + b"\x04\xce\0\x1e"
            + mqtt_string("test-robot")
            + mqtt_string("private-topic")
            + b"\0\x02\xff\0"
            + mqtt_string("private-username")
            + b"\0\x02\xfe\0"
        )
        result = probe.connect_metadata(0x10, body)
        self.assertTrue(result["will_present"])
        self.assertEqual(result["will_qos"], 1)
        self.assertEqual(result["will_payload_bytes"], 2)
        self.assertEqual(result["password_bytes"], 2)
        self.assertNotIn("private", json.dumps(result))

    def test_mqtt5_properties_are_not_logged(self):
        body = (
            mqtt_string("MQTT")
            + b"\x05\x06\0\x1e"
            + b"\x04\x15\0\x01x"
            + mqtt_string("test-robot")
            + b"\x04\x03\0\x01y"
            + mqtt_string("private-topic")
            + mqtt_string("private-payload")
        )
        result = probe.connect_metadata(0x10, body)
        self.assertEqual(result["protocol_version"], 5)
        self.assertEqual(result["will_payload_bytes"], 15)
        self.assertNotIn("private", json.dumps(result))

    def test_malformed_connect_is_rejected_without_payload_in_error(self):
        valid = connect_body()
        malformed = [
            (0x30, valid),
            (0x11, valid),
            (0x10, valid + b"x"),
            (0x10, connect_body(flags=3)),
            (0x10, connect_body(flags=0x42)),
            (0x10, connect_body(flags=0x1C)),
        ]
        malformed += [(0x10, valid[:length]) for length in range(len(valid))]
        for header, body in malformed:
            with self.subTest(header=header, length=len(body)):
                with self.assertRaises(ProtocolError) as caught:
                    probe.connect_metadata(header, body)
                self.assertNotIn("private", str(caught.exception))

    def test_fragmented_connect(self):
        wire = packet(0x10, connect_body())
        with patch.object(probe.time, "monotonic", return_value=1):
            from unittest.mock import Mock

            conn = Mock()
            conn.recv.side_effect = [wire[:1], wire[1:4], wire[4:]]
            self.assertEqual(probe.read_connect(conn, 10)["client_id"], "test-robot")

    def test_closed_and_timed_out_connections(self):
        from unittest.mock import Mock

        conn = Mock()
        conn.recv.return_value = b""
        with self.assertRaises(ConnectionError):
            probe.read_connect(conn, 10)
        with patch.object(probe.time, "monotonic", side_effect=[1, 12]):
            with self.assertRaises(TimeoutError):
                probe.read_connect(conn, 10)


class MapDetailsDispatchTests(unittest.TestCase):
    def test_map_details_response_and_deferred_matter_request(self):
        robot_id = "test-robot"
        base_topic = f"v011-irbthbu/things/{robot_id}"
        request_topic = base_topic + "/mapdetails/req"
        response_topic = base_topic + "/mapdetails/resp"
        matter_topic = base_topic + "/matter/fabric/req"
        payload = b'{"request_id":7,"p2map_ids":["map-one"]}'
        document = {
            "request_id": 7,
            "maps": [
                {
                    "p2map_id": "map-one",
                    "p2mapv_id": "1.0",
                    "regions": [{"region_id": "1", "region_type": "rid", "name": "Kitchen"}],
                }
            ],
        }
        shadows = Mock()
        shadows.path = "shadows.db"
        shadows.respond.return_value = None
        map_uploads = Mock()
        map_uploads.path = "uploads.db"
        output = io.StringIO()
        timeout = 5
        buffer = bytearray()

        server, client = socket.socketpair()
        with server, client:
            client.settimeout(timeout)
            observer = threading.Thread(
                target=probe.observe,
                args=(
                    server,
                    "127.0.0.1",
                    {"keepalive_seconds": 30, "protocol_version": 4, "client_id": robot_id},
                    bytearray(),
                    None,
                ),
                kwargs={"shadows": shadows, "map_uploads": map_uploads},
            )
            with (
                patch.object(
                    probe,
                    "respond_map_details",
                    return_value=(response_topic, document),
                ) as responder,
                contextlib.redirect_stdout(output),
            ):
                observer.start()
                try:
                    client.sendall(packet(0x82, b"\0\x01" + mqtt_string(response_topic) + b"\0"))
                    self.assertEqual(
                        probe.read_frame(client, timeout, buffer),
                        (0x90, b"\0\x01\0"),
                    )
                    client.sendall(
                        packet(
                            0x32,
                            mqtt_string(request_topic) + b"\0\x02" + payload,
                        )
                    )
                    self.assertEqual(
                        probe.read_frame(client, timeout, buffer),
                        (0x40, b"\0\x02"),
                    )
                    header, body = probe.read_frame(client, timeout, buffer)
                    self.assertEqual(header, 0x30)
                    reader = probe.PacketReader(body)
                    self.assertEqual(reader.text(), response_topic)
                    self.assertEqual(json.loads(body[reader.pos :]), document)
                    responder.assert_called_once_with(
                        "shadows.db",
                        "uploads.db",
                        request_topic,
                        payload,
                        robot_id,
                    )

                    client.sendall(
                        packet(
                            0x32,
                            mqtt_string(matter_topic) + b"\0\x03" + b'{"request_id":1}',
                        )
                    )
                    self.assertEqual(
                        probe.read_frame(client, timeout, buffer),
                        (0x40, b"\0\x03"),
                    )
                    client.sendall(packet(0xE0, b""))
                    observer.join(timeout)
                    self.assertFalse(observer.is_alive())
                finally:
                    if observer.is_alive():
                        client.shutdown(socket.SHUT_RDWR)
                        observer.join(timeout)

        logs = output.getvalue()
        self.assertIn("mapdetails_response_sent", logs)
        self.assertIn("matter_fabric_request_unsupported", logs)


class TLSIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build_directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.build_directory.cleanup)
        cls.bridge = Path(cls.build_directory.name) / "mqtt_tls_bridge"
        subprocess.run(
            [
                "cc",
                "-O2",
                "-Wall",
                "-Wextra",
                "-Werror",
                str(Path(probe.__file__).resolve().parents[1] / "native/mqtt_tls_bridge.c"),
                "-o",
                str(cls.bridge),
                "-lssl",
                "-lcrypto",
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    def test_trusted_tls_connect_is_logged_redacted_and_rejected(self):
        self.check_tls_connect("1.2", ssl.TLSVersion.TLSv1_2, 4)

    def test_tls13_with_mqtt5_is_logged_redacted_and_rejected(self):
        self.check_tls_connect("1.3", ssl.TLSVersion.TLSv1_3, 5)

    def test_client_certificate_pkcs1_mqtt5(self):
        self.check_tls_connect("1.2", ssl.TLSVersion.TLSv1_2, 5, client_auth=True)

    def test_client_certificate_is_optional(self):
        self.check_tls_connect(
            "1.2", ssl.TLSVersion.TLSv1_2, 4, client_auth=True, present_certificate=False
        )

    def test_observation_over_client_certificate_transport(self):
        self.check_tls_connect("1.2", ssl.TLSVersion.TLSv1_2, 5, client_auth=True, observation=True)

    def test_observation_over_original_transport(self):
        self.check_tls_connect("1.2", ssl.TLSVersion.TLSv1_2, 4, observation=True)

    def test_private_capture_over_client_certificate_transport(self):
        self.check_tls_connect(
            "1.2",
            ssl.TLSVersion.TLSv1_2,
            5,
            client_auth=True,
            observation=True,
            capture_payloads=True,
        )

    def test_shadow_responses_over_client_certificate_transport(self):
        self.check_tls_connect(
            "1.2",
            ssl.TLSVersion.TLSv1_2,
            5,
            client_auth=True,
            observation=True,
            shadow_service=True,
        )

    def check_tls_connect(
        self,
        maximum,
        client_version,
        mqtt_version,
        client_auth=False,
        present_certificate=True,
        observation=False,
        capture_payloads=False,
        shadow_service=False,
    ):
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as resources:
            cert, key = make_cert(directory, ["localhost"])
            capture_path = Path(directory) / "payloads.jsonl"
            capture = (
                resources.enter_context(probe.PrivateCapture(capture_path))
                if capture_payloads
                else None
            )
            shadows = (
                probe.ShadowService(Path(directory) / "shadows.db") if shadow_service else None
            )
            server_context = probe.tls_context(cert, key, maximum)
            self.assertEqual(server_context.minimum_version, ssl.TLSVersion.TLSv1_2)
            self.assertEqual(server_context.maximum_version, client_version)
            self.assertEqual(server_context.verify_mode, ssl.CERT_NONE)
            client_context = ssl.create_default_context(cafile=str(cert))
            client_context.minimum_version = client_context.maximum_version = client_version
            auth = None
            if client_auth:
                auth = (cert, key, self.bridge)
                if present_certificate:
                    with tempfile.TemporaryDirectory() as client_directory:
                        client_cert, client_key = make_cert(client_directory, ["private-client"])
                        client_context.load_cert_chain(client_cert, client_key)
            output = io.StringIO()
            with socket.socket() as listener, contextlib.redirect_stdout(output):
                listener.bind(("127.0.0.1", 0))
                listener.listen()
                listener.settimeout(5)
                errors = []

                def server():
                    try:
                        raw, peer = listener.accept()
                        probe.handle(
                            raw,
                            peer,
                            server_context,
                            client_auth=auth,
                            observe_seconds=5 if observation else None,
                            capture=capture,
                            shadows=shadows,
                        )
                    except Exception as exc:
                        errors.append(exc)

                thread = threading.Thread(target=server, daemon=True)
                thread.start()
                try:
                    with socket.create_connection(listener.getsockname(), timeout=5) as raw:
                        with client_context.wrap_socket(raw, server_hostname="localhost") as conn:
                            wire = packet(0x10, connect_body(mqtt_version))
                            conn.sendall(wire[:3])
                            subscription = b"\0\x01" + (b"\0" if mqtt_version == 5 else b"")
                            subscription += mqtt_string("$aws/things/test-robot/shadow/#") + b"\x01"
                            conn.sendall(
                                wire[3:] + (packet(0x82, subscription) if observation else b"")
                            )
                            response = bytearray()
                            expected = (
                                probe.successful_connack(mqtt_version)
                                if observation
                                else probe.unavailable_connack(mqtt_version)
                            )
                            while len(response) < len(expected):
                                chunk = conn.recv(len(expected) - len(response))
                                self.assertTrue(chunk)
                                response.extend(chunk)
                            self.assertEqual(response, expected)
                            if observation:

                                def receive(expected):
                                    data = bytearray()
                                    while len(data) < len(expected):
                                        chunk = conn.recv(len(expected) - len(data))
                                        self.assertTrue(chunk)
                                        data.extend(chunk)
                                    self.assertEqual(bytes(data), expected)

                                receive(
                                    packet(
                                        0x90,
                                        b"\0\x01" + (b"\0" if mqtt_version == 5 else b"") + b"\x01",
                                    )
                                )
                                publication_topic = (
                                    "state"
                                    if shadow_service
                                    else "$aws/things/test-robot/shadow/name/ro-currentstate/update"
                                )
                                publication = mqtt_string(publication_topic) + b"\0\x02"
                                publication += (
                                    b"\0" if mqtt_version == 5 else b""
                                ) + b"private-payload"
                                conn.sendall(packet(0x32, publication) + b"\xc0\0")
                                receive(b"\x40\x02\0\x02\xd0\0")
                                if shadow_service:
                                    base = "$aws/things/test-robot/shadow/name/ro-currentstate/"

                                    def shadow_request(action, value):
                                        payload = json.dumps(value).encode()
                                        conn.sendall(
                                            packet(
                                                0x30, mqtt_string(base + action) + b"\0" + payload
                                            )
                                        )
                                        buffered = bytearray()
                                        while (frame := probe.extract_packet(buffered)) is None:
                                            chunk = conn.recv(8192)
                                            self.assertTrue(chunk)
                                            buffered.extend(chunk)
                                        self.assertEqual(frame[0], 0x30)
                                        reader = probe.PacketReader(frame[1])
                                        topic = reader.text()
                                        reader.properties()
                                        result = json.loads(reader.take(len(frame[1]) - reader.pos))
                                        return topic, result

                                    topic, result = shadow_request(
                                        "get", {"clientToken": "private-token"}
                                    )
                                    self.assertEqual(topic, base + "get/rejected")
                                    self.assertEqual(result["code"], 404)
                                    self.assertEqual(result["clientToken"], "private-token")
                                    topic, result = shadow_request(
                                        "update", {"state": {"reported": {"batPct": 72}}}
                                    )
                                    self.assertEqual(topic, base + "update/accepted")
                                    self.assertEqual(result["version"], 1)
                                    topic, result = shadow_request(
                                        "get", {"clientToken": "private-token"}
                                    )
                                    self.assertEqual(topic, base + "get/accepted")
                                    self.assertEqual(result["state"]["reported"]["batPct"], 72)
                                conn.sendall(b"\xe0\0")
                            self.assertEqual(conn.recv(1), b"")
                finally:
                    thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
            logs = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertIn("mqtt_connect", [entry["event"] for entry in logs])
            self.assertIn(
                "mqtt_disconnect" if observation else "mqtt_rejected",
                [entry["event"] for entry in logs],
            )
            handshake = next(entry for entry in logs if entry["event"] == "handshake_ok")
            self.assertEqual(handshake["tls"], "TLSv" + maximum)
            if client_auth:
                self.assertEqual(handshake["client_certificate_present"], present_certificate)
                self.assertEqual(
                    handshake["client_signature_type"],
                    "rsaEncryption" if present_certificate else "none",
                )
                self.assertEqual(
                    handshake["client_signature_digest"],
                    "SHA256" if present_certificate else "none",
                )
                self.assertNotIn("private-client", output.getvalue())
            self.assertNotIn("private-username", output.getvalue())
            self.assertNotIn("private-password", output.getvalue())
            self.assertNotIn("private-payload", output.getvalue())
            self.assertNotIn("private-token", output.getvalue())
            if capture_payloads:
                records = [json.loads(line) for line in capture_path.read_text().splitlines()]
                self.assertEqual(len(records), 1)
                self.assertEqual(base64.b64decode(records[0]["payload_base64"]), b"private-payload")
                self.assertNotIn("private-username", capture_path.read_text())
                self.assertNotIn("private-password", capture_path.read_text())
                self.assertIn("mqtt_payload_saved", [entry["event"] for entry in logs])

    def test_client_auth_handshake_failure_is_reported_and_cleaned_up(self):
        with tempfile.TemporaryDirectory() as directory:
            cert, key = make_cert(directory, ["localhost"])
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                listener.listen()
                with socket.create_connection(listener.getsockname(), timeout=5) as client:
                    raw, peer = listener.accept()
                    client.sendall(b"invalid-private-test-data\n")
                    client.shutdown(socket.SHUT_WR)
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        probe.handle(
                            raw, peer, None, timeout=2, client_auth=(cert, key, self.bridge)
                        )
            logs = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertIn("handshake_failed", [entry["event"] for entry in logs])
            self.assertNotIn("handshake_ok", [entry["event"] for entry in logs])
            self.assertNotIn("invalid-private-test-data", output.getvalue())
