import base64
import contextlib
import io
import json
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import roomba_cloud.robot_mqtt as poc


class FakeTransport:
    timeout = 0.1

    def __init__(self, frames=()):
        self.frames = iter(frames)
        self.sent = []

    def send(self, data):
        self.sent.append(data)

    def receive(self, timeout=None):
        return next(self.frames)


class ProtocolTests(unittest.TestCase):
    def test_fragmented_and_coalesced_frames(self):
        first = poc.packet(0x30, b"x" * 200)
        second = poc.packet(0xD0)
        buffer = bytearray(first[:2])
        self.assertIsNone(poc.extract_packet(buffer))
        buffer.extend(first[2:-1])
        self.assertIsNone(poc.extract_packet(buffer))
        buffer.extend(first[-1:] + second)
        self.assertEqual(poc.extract_packet(buffer), (0x30, b"x" * 200))
        self.assertEqual(poc.extract_packet(buffer), (0xD0, b""))
        self.assertEqual(buffer, b"")

    def test_invalid_frame_lengths(self):
        for data in (b"\x30\xff\xff\xff\xff", b"\x30\xff\xff\x7f"):
            with self.subTest(data=data), self.assertRaises(poc.ProtocolError):
                poc.extract_packet(bytearray(data))
        with self.assertRaises(ValueError):
            poc.packet(0x30, bytes(poc.MAX_PACKET + 1))

    def test_commands(self):
        with patch.object(poc.time, "time", return_value=100):
            self.assertEqual(
                poc.command_payload("start"),
                {
                    "command": "start",
                    "time": 100,
                    "initiator": "localApp",
                    "params": {"operatingMode": 2},
                },
            )
            self.assertEqual(
                poc.command_payload("start", True, 3)["params"],
                {
                    "operatingMode": 6,
                    "padWetness": {"disposable": 3, "reusable": 3},
                },
            )
            for command in ("stop", "dock", "pause", "resume"):
                self.assertEqual(
                    poc.command_payload(command),
                    {
                        "command": command,
                        "time": 100,
                        "initiator": "localApp",
                    },
                )

    def test_login_uses_separate_password(self):
        transport = FakeTransport([(0x20, b"\0\0")])
        poc.MQTT(transport).connect("robot-id", "secret")
        header, body = poc.extract_packet(bytearray(transport.sent[0]))
        self.assertEqual(header, 0x10)
        self.assertEqual(
            body,
            (
                b"\0\x04MQTT\x04\xc2\0\x1e"
                + poc.mqtt_string("robot-id") * 2
                + poc.mqtt_string("secret")
            ),
        )

    def test_login_rejection(self):
        for frame in ((0x20, b"\0\x04"), (0x20, b"\x01\0"), (0x30, b"\0\0")):
            with self.subTest(frame=frame), self.assertRaises(poc.ProtocolError):
                poc.MQTT(FakeTransport([frame])).connect("robot", "secret")

    def test_subscription_rejection(self):
        with self.assertRaisesRegex(poc.ProtocolError, "Subscription rejected"):
            poc.MQTT(FakeTransport([(0x90, b"\0\x01\x80")])).subscribe("#")

    def test_incoming_qos1_preserves_bytes_and_acks(self):
        transport = FakeTransport()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            poc.MQTT(transport).incoming(
                0x33,
                poc.mqtt_string("state") + b"\0\x09" + b"\xff\0test",
            )
        message = json.loads(output.getvalue())
        self.assertEqual(base64.b64decode(message["payload_base64"]), b"\xff\0test")
        self.assertTrue(message["retained"])
        self.assertEqual(transport.sent, [b"\x40\x02\0\x09"])

    def test_invalid_publications(self):
        for header, body in (
            (0x34, poc.mqtt_string("state")),
            (0x30, b"\0\x10short"),
            (0x32, poc.mqtt_string("state") + b"\0\0"),
        ):
            with self.subTest(header=header), self.assertRaises(poc.ProtocolError):
                poc.MQTT(FakeTransport()).incoming(header, body)

    def test_dry_run_never_opens_transport(self):
        with (
            patch("sys.argv", ["robot_mqtt.py", "start", "--mop", "--wetness", "1"]),
            patch.object(poc, "TLS") as transport,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            poc.main()
        transport.assert_not_called()
        data = json.loads(output.getvalue())
        self.assertTrue(data["dry_run"])
        self.assertEqual(data["payload"]["params"]["padWetness"]["reusable"], 1)

    def test_cli_rejects_wetness_without_mop(self):
        with (
            patch("sys.argv", ["robot_mqtt.py", "start", "--wetness", "1"]),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            poc.main()

    def test_positive_finite_durations(self):
        for value in ("0", "-1", "nan", "inf"):
            with self.subTest(value=value), self.assertRaises(poc.argparse.ArgumentTypeError):
                poc.positive_seconds(value)

    def test_transport_requires_explicit_trust_choice(self):
        with self.assertRaisesRegex(ValueError, "Choose --ca-file"):
            poc.TLS("127.0.0.1", 8883, 1)

    def test_cli_rejects_connection_without_trust_choice(self):
        with (
            patch(
                "sys.argv",
                [
                    "robot_mqtt.py",
                    "listen",
                    "--ip",
                    "127.0.0.1",
                    "--blid",
                    "test",
                ],
            ),
            contextlib.redirect_stderr(io.StringIO()),
            patch.object(poc, "TLS") as transport,
            self.assertRaises(SystemExit),
        ):
            poc.main()
        transport.assert_not_called()


class TLSIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        cls.cert = Path(cls.directory.name) / "cert.pem"
        cls.key = Path(cls.directory.name) / "key.pem"
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(cls.key),
                "-out",
                str(cls.cert),
                "-days",
                "1",
                "-subj",
                "/CN=127.0.0.1",
                "-addext",
                "subjectAltName=IP:127.0.0.1",
            ],
            check=True,
            capture_output=True,
            timeout=15,
        )

    def test_real_openssl_transport_against_tls_broker(self):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = context.maximum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(self.cert, self.key)
        errors = []
        received = []
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            listener.settimeout(5)

            def server():
                try:
                    raw, _ = listener.accept()
                    raw.settimeout(5)
                    with context.wrap_socket(raw, server_side=True) as connection:
                        buffer = bytearray()

                        def read():
                            while True:
                                frame = poc.extract_packet(buffer)
                                if frame is not None:
                                    received.append(frame)
                                    return frame
                                data = connection.recv(65536)
                                if not data:
                                    raise ConnectionError("Mock broker client disconnected")
                                buffer.extend(data)

                        self.assertEqual(read()[0], 0x10)
                        # Exercise a read timeout midway through a frame.
                        connection.sendall(b"\x20")
                        time.sleep(0.1)
                        connection.sendall(b"\x02\0\0")
                        for _ in range(2):
                            header, body = read()
                            self.assertEqual(header, 0x82)
                            connection.sendall(poc.packet(0x90, body[:2] + b"\x01"))
                        self.assertEqual(read()[0], 0x30)
                        connection.sendall(
                            poc.packet(
                                0x32,
                                poc.mqtt_string("state") + b"\0\x07" + b'{"test":true}',
                            )
                        )
                        self.assertEqual(read(), (0x40, b"\0\x07"))
                        self.assertEqual(read(), (0xC0, b""))
                        connection.sendall(poc.packet(0xD0))
                        self.assertEqual(read(), (0xE0, b""))
                except Exception as exc:
                    errors.append(exc)

            thread = threading.Thread(target=server, daemon=True)
            thread.start()
            transport = poc.TLS(
                "127.0.0.1",
                listener.getsockname()[1],
                3,
                ca_file=self.cert,
            )
            try:
                client = poc.MQTT(transport)
                with (
                    contextlib.redirect_stderr(io.StringIO()),
                    contextlib.redirect_stdout(io.StringIO()) as output,
                ):
                    client.connect("test-robot", "test-password")
                    client.subscribe("#")
                    client.subscribe("$aws/#")
                    client.command(poc.command_payload("pause"))
                    client.listen(0.2)
                    transport.send(poc.packet(0xC0))
                    self.assertEqual(transport.receive(), (0xD0, b""))
                    transport.send(poc.packet(0xE0))
                self.assertEqual(json.loads(output.getvalue())["topic"], "state")
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive(), "Mock broker did not finish")
                if errors:
                    raise errors[0]
                self.assertEqual(len(received), 7)
            finally:
                transport.close()

    def test_closed_port_has_bounded_diagnostic_failure(self):
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            transport = poc.TLS(
                "127.0.0.1",
                reserved.getsockname()[1],
                2,
                insecure=True,
            )
            try:
                with self.assertRaisesRegex(ConnectionError, "OpenSSL diagnostics"):
                    transport.receive()
            finally:
                transport.close()


if __name__ == "__main__":
    unittest.main()
