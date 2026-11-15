import base64
import contextlib
import getpass
import io
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, Mock, patch

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

    def test_mqtt_string_rejects_empty_nul_and_oversized_values(self):
        for value in ("", "nul\0inside", "x" * 65536):
            with self.subTest(value_length=len(value)), self.assertRaises(ValueError):
                poc.mqtt_string(value)

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

    def test_login_reports_all_broker_rejection_codes(self):
        reasons = {
            1: "protocol version rejected",
            2: "client ID rejected",
            3: "server unavailable",
            4: "bad username/password",
            5: "not authorized",
            6: "6",
        }
        for code, reason in reasons.items():
            with self.subTest(code=code), self.assertRaisesRegex(poc.ProtocolError, reason):
                poc.MQTT(FakeTransport([(0x20, b"\0" + bytes([code]))])).connect(
                    "robot", "secret"
                )

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
            (0x20, b""),
            (0x30, b"\0"),
            (0x34, poc.mqtt_string("state") + b"\0"),
            (0x34, poc.mqtt_string("state")),
            (0x30, b"\0\x10short"),
            (0x32, poc.mqtt_string("state") + b"\0\0"),
            (0x30, poc.mqtt_string("state/#") + b"payload"),
        ):
            with self.subTest(header=header), self.assertRaises(poc.ProtocolError):
                poc.MQTT(FakeTransport()).incoming(header, body)

    def test_incoming_qos0_prints_payload_and_qos1_rejects_invalid_identifiers(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            poc.MQTT(FakeTransport()).incoming(
                0x38, poc.mqtt_string("state") + b'{"ok":true}'
            )
        message = json.loads(output.getvalue())
        self.assertTrue(message["duplicate"])
        self.assertEqual(message["payload_text"], '{"ok":true}')
        self.assertFalse(message["retained"])
        for identifier in (b"\0\0",):
            with self.subTest(identifier=identifier), self.assertRaisesRegex(
                poc.ProtocolError, "packet identifier"
            ):
                poc.MQTT(FakeTransport()).incoming(
                    0x32, poc.mqtt_string("state") + identifier + b"payload"
                )
        with self.assertRaises(UnicodeDecodeError):
            poc.MQTT(FakeTransport()).incoming(0x30, b"\0\x01\xff")

    def test_subscribe_accepts_ack_and_processes_interleaved_publications(self):
        transport = FakeTransport(
            [
                (
                    0x30,
                    poc.mqtt_string("state") + b'{"online":true}',
                ),
                (0x90, b"\0\x01\x01"),
            ]
        )
        output = io.StringIO()
        errors = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            poc.MQTT(transport).subscribe("state")
        self.assertEqual(json.loads(output.getvalue())["topic"], "state")
        self.assertIn("Subscription accepted: state (QoS 1)", errors.getvalue())
        self.assertEqual(len(transport.sent), 1)

    def test_subscribe_rejects_malformed_acknowledgements_and_times_out(self):
        for body in (b"\0", b"\0\x02\0", b"\0\x01\x80", b"\0\x02\0"):
            with self.subTest(body=body), self.assertRaisesRegex(
                poc.ProtocolError, "SUBACK"
            ):
                poc.MQTT(FakeTransport([(0x90, body)])).subscribe("state")
        client = poc.MQTT(FakeTransport())
        with (
            patch.object(poc.time, "monotonic", side_effect=[0, 0.2]),
            self.assertRaisesRegex(TimeoutError, "SUBACK"),
        ):
            client.subscribe("state")

    def test_listen_validates_ping_responses_and_ping_deadlines(self):
        client = poc.MQTT(FakeTransport([(0xD0, b"")]))
        with (
            patch.object(poc.time, "monotonic", side_effect=[0, 0, 0, 0, 0, 0, 1]),
            self.assertRaisesRegex(poc.ProtocolError, "unsolicited PINGRESP"),
        ):
            client.listen(0.5)

        transport = FakeTransport()
        client = poc.MQTT(transport)
        with patch.object(
            poc.time, "monotonic", side_effect=[0, 0, 0, 11, 11, 11, 21, 21]
        ), patch.object(transport, "receive", return_value=(0xD0, b"")):
            client.listen(20)
        self.assertEqual(transport.sent, [b"\xc0\x00"])

        transport = FakeTransport()
        client = poc.MQTT(transport)
        with (
            patch.object(poc.time, "monotonic", side_effect=[0, 0, 0, 11, 11, 11, 12, 12]),
            patch.object(transport, "receive", side_effect=TimeoutError),
            self.assertRaisesRegex(TimeoutError, "PINGREQ"),
        ):
            client.listen(20)

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

    def test_listener_handles_interrupted_writes_timeouts_and_closed_transport(self):
        process = Mock()
        process.stdin = Mock()
        process.stdin.fileno.return_value = 11
        process.stdout = Mock()
        process.stdout.fileno.return_value = 12
        errors = io.BytesIO(b"broker closed")
        with (
            patch.object(poc.subprocess, "Popen", return_value=process),
            patch.object(poc.tempfile, "TemporaryFile", return_value=errors),
            patch.object(poc.os, "set_blocking"),
        ):
            transport = poc.TLS("127.0.0.1", 8883, 1, insecure=True)

        with (
            patch.object(poc.select, "select", return_value=([], [process.stdin], [])),
            patch.object(
                poc.os,
                "write",
                side_effect=[BlockingIOError(), 2, 2],
            ) as write,
        ):
            transport.send(b"data")
        self.assertEqual(
            [call.args[1] for call in write.call_args_list], [b"data", b"data", b"ta"]
        )

        with (
            patch.object(poc.time, "monotonic", side_effect=[0, 2]),
            patch.object(poc.select, "select", return_value=([], [], [])),
            self.assertRaisesRegex(TimeoutError, "writing"),
        ):
            transport.send(b"x")

        with (
            patch.object(poc.select, "select", return_value=([], [process.stdin], [])),
            patch.object(poc.os, "write", side_effect=BrokenPipeError),
            patch.object(transport, "failure", return_value=ConnectionError("closed")),
            self.assertRaisesRegex(ConnectionError, "closed"),
        ):
            transport.send(b"x")

        transport.buffer.extend(poc.packet(0xD0))
        self.assertEqual(transport.receive(), (0xD0, b""))
        with (
            patch.object(poc.time, "monotonic", side_effect=[0, 2]),
            self.assertRaisesRegex(TimeoutError, "waiting"),
        ):
            transport.receive()
        with (
            patch.object(poc.select, "select", return_value=([process.stdout], [], [])),
            patch.object(poc.os, "read", return_value=b""),
            patch.object(transport, "failure", return_value=ConnectionError("broker closed")),
            self.assertRaisesRegex(ConnectionError, "broker closed"),
        ):
            transport.receive()
        transport.errors = errors
        process.poll.return_value = None
        process.wait.side_effect = [
            subprocess.TimeoutExpired("openssl", 3),
            None,
        ]
        transport.close()
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        process.stdin.close.assert_called_once()
        process.stdout.close.assert_called_once()
        self.assertTrue(errors.closed)

    def test_tls_constructor_closes_diagnostics_when_openssl_cannot_start(self):
        errors = Mock()
        with (
            patch.object(poc.tempfile, "TemporaryFile", return_value=errors),
            patch.object(poc.subprocess, "Popen", side_effect=OSError("openssl missing")),
            self.assertRaisesRegex(OSError, "openssl missing"),
        ):
            poc.TLS("127.0.0.1", 8883, 1, insecure=True)
        errors.close.assert_called_once()


class DiscoveryAndMainTests(unittest.TestCase):
    def test_discovery_accepts_plain_and_length_prefixed_json_and_ignores_bad_replies(self):
        sock = MagicMock()
        sock.__enter__.return_value = sock
        valid = json.dumps({"robot": "one"}).encode()
        prefixed = json.dumps({"robot": "two"}).encode()
        sock.recvfrom.side_effect = [
            (b"not-json", ("192.0.2.1", 5678)),
            (valid, ("192.0.2.2", 5678)),
            (struct.pack("!H", len(prefixed)) + prefixed, ("192.0.2.3", 5678)),
            socket.timeout,
        ]
        output, errors = io.StringIO(), io.StringIO()
        with (
            patch.object(poc.socket, "socket", return_value=sock),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            poc.discover("192.0.2.255", 1)
        self.assertEqual(
            [json.loads(line)["ip"] for line in output.getvalue().splitlines()],
            ["192.0.2.2", "192.0.2.3"],
        )
        self.assertIn("Invalid discovery response from 192.0.2.1", errors.getvalue())
        sock.setsockopt.assert_called_once_with(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto.assert_called_once_with(b"irobotmcs", ("192.0.2.255", 5678))

    def test_discovery_deduplicates_peers_and_reports_no_responses(self):
        duplicate = MagicMock()
        duplicate.__enter__.return_value = duplicate
        duplicate.recvfrom.side_effect = [
            (b'{"robot":1}', ("192.0.2.4", 5678)),
            (b"malformed duplicate", ("192.0.2.4", 5678)),
            socket.timeout,
        ]
        with patch.object(poc.socket, "socket", return_value=duplicate):
            with contextlib.redirect_stdout(io.StringIO()):
                poc.discover("192.0.2.255", 1)
        duplicate.recvfrom.assert_called()

        empty = MagicMock()
        empty.__enter__.return_value = empty
        empty.recvfrom.side_effect = socket.timeout
        with patch.object(poc.socket, "socket", return_value=empty):
            with self.assertRaisesRegex(TimeoutError, "No discovery responses"):
                poc.discover("192.0.2.255", 1)
        immediate = MagicMock()
        immediate.__enter__.return_value = immediate
        with (
            patch.object(poc.socket, "socket", return_value=immediate),
            patch.object(poc.time, "monotonic", side_effect=[0, 0]),
            self.assertRaisesRegex(TimeoutError, "No discovery responses"),
        ):
            poc.discover("192.0.2.255", 0)
        immediate.recvfrom.assert_not_called()
        with self.assertRaises(ValueError):
            poc.discover("not-an-ip", 1)

    def test_discovery_rejects_short_lengths_and_non_object_json(self):
        sock = MagicMock()
        sock.__enter__.return_value = sock
        non_object = b"[]"
        sock.recvfrom.side_effect = [
            (b"\0", ("192.0.2.5", 5678)),
            (struct.pack("!H", len(non_object)) + non_object, ("192.0.2.6", 5678)),
            (b'{"robot":true}', ("192.0.2.7", 5678)),
            socket.timeout,
        ]
        output, errors = io.StringIO(), io.StringIO()
        with (
            patch.object(poc.socket, "socket", return_value=sock),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            poc.discover("192.0.2.255", 1)
        self.assertIn("truncated discovery length", errors.getvalue())
        self.assertIn("response is not an object", errors.getvalue())
        self.assertEqual(json.loads(output.getvalue())["ip"], "192.0.2.7")

    def test_discover_cli_dispatches_validated_target_and_timeout(self):
        with (
            patch("sys.argv", ["robot_mqtt.py", "discover", "--target", "192.0.2.255", "--timeout", "2.5"]),
            patch.object(poc, "discover") as discover,
        ):
            poc.main()
        discover.assert_called_once_with("192.0.2.255", 2.5)

    def test_executed_command_uses_password_file_and_closes_transport(self):
        with tempfile.TemporaryDirectory() as directory:
            password_file = Path(directory) / "robot-password"
            password_file.write_text("private-password\n")
            transport = Mock()
            client = Mock()
            wake = MagicMock()
            wake.__enter__.return_value = wake
            argv = [
                "robot_mqtt.py",
                "start",
                "--ip",
                "127.0.0.1",
                "--blid",
                "robot",
                "--insecure",
                "--execute",
                "--password-file",
                str(password_file),
                "--topic",
                "state",
            ]
            errors = io.StringIO()
            with (
                patch("sys.argv", argv),
                patch.object(poc.socket, "socket", return_value=wake) as socket_create,
                patch.object(poc.time, "sleep"),
                patch.object(poc, "TLS", return_value=transport) as tls,
                patch.object(poc, "MQTT", return_value=client),
                contextlib.redirect_stderr(errors),
            ):
                poc.main()
            self.assertEqual(socket_create.call_args.args[0], socket.AF_INET)
            wake.sendto.assert_called_once_with(b"irobotmcs", ("127.0.0.1", 5678))
            tls.assert_called_once_with("127.0.0.1", 8883, 10, None, True)
            client.connect.assert_called_once_with("robot", "private-password")
            client.subscribe.assert_called_once_with("state")
            self.assertEqual(client.command.call_args.args[0]["command"], "start")
            client.listen.assert_called_once_with(2)
            transport.send.assert_called_once_with(poc.packet(0xE0))
            transport.close.assert_called_once()
            self.assertIn("NOT authenticated", errors.getvalue())

    def test_execution_validates_connection_and_reports_runtime_errors(self):
        for extra, message in (
            (["--ip", "127.0.0.1"], "Set --ip/--blid"),
            (["--ip", "127.0.0.1", "--blid", "robot", "--port", "0", "--insecure"], "port"),
        ):
            with self.subTest(extra=extra):
                stderr = io.StringIO()
                with (
                    patch("sys.argv", ["robot_mqtt.py", "listen", *extra]),
                    contextlib.redirect_stderr(stderr),
                    self.assertRaises(SystemExit),
                ):
                    poc.main()
                self.assertIn(message, stderr.getvalue())
        with (
            patch(
                "sys.argv",
                [
                    "robot_mqtt.py",
                    "listen",
                    "--ip",
                    "not-an-ip",
                    "--blid",
                    "robot",
                    "--insecure",
                ],
            ),
            self.assertRaisesRegex(ValueError, "does not appear to be"),
        ):
            poc.main()

        argv = [
            str(Path(poc.__file__)),
            "listen",
            "--ip",
            "127.0.0.1",
            "--blid",
            "robot",
            "--insecure",
        ]
        errors = io.StringIO()
        with (
            patch("sys.argv", argv),
            patch.dict(os.environ, {"ROOMBA_PASSWORD": "private-password"}, clear=True),
            patch.object(poc.socket, "socket", side_effect=OSError("wake failed")),
            contextlib.redirect_stderr(errors),
            self.assertRaises(SystemExit) as raised,
        ):
            import runpy

            runpy.run_path(poc.__file__, run_name="__main__")
        self.assertEqual(raised.exception.code, 1)
        self.assertIn("Error: wake failed", errors.getvalue())

    def test_ipv6_listen_prompts_for_password_and_uses_default_subscriptions(self):
        transport = Mock()
        client = Mock()
        wake = MagicMock()
        wake.__enter__.return_value = wake
        with (
            patch("sys.argv", ["robot_mqtt.py", "listen", "--ip", "::1", "--blid", "robot", "--ca-file", "ca.pem"]),
            patch.dict(os.environ, {}, clear=True),
            patch.object(poc.socket, "socket", return_value=wake) as socket_create,
            patch.object(poc.time, "sleep"),
            patch.object(poc, "TLS", return_value=transport) as tls,
            patch.object(poc, "MQTT", return_value=client),
            patch.object(getpass, "getpass", return_value="prompt-password") as prompt,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            poc.main()
        self.assertEqual(socket_create.call_args.args, (socket.AF_INET6, socket.SOCK_DGRAM))
        prompt.assert_called_once_with("Robot MQTT password (not account password): ")
        tls.assert_called_once_with("::1", 8883, 10, Path("ca.pem"), False)
        client.connect.assert_called_once_with("robot", "prompt-password")
        self.assertEqual(
            client.subscribe.call_args_list,
            [unittest.mock.call("#"), unittest.mock.call("$aws/#")],
        )
        client.command.assert_not_called()
        transport.close.assert_called_once()

    def test_top_level_keyboard_interrupt_is_reported_as_exit_130(self):
        with (
            patch(
                "sys.argv",
                [
                    str(Path(poc.__file__)),
                    "listen",
                    "--ip",
                    "127.0.0.1",
                    "--blid",
                    "robot",
                    "--insecure",
                ],
            ),
            patch.dict(os.environ, {"ROOMBA_PASSWORD": "private-password"}, clear=True),
            patch.object(poc.socket, "socket", side_effect=KeyboardInterrupt),
            contextlib.redirect_stderr(io.StringIO()) as errors,
            self.assertRaises(SystemExit) as raised,
        ):
            import runpy

            runpy.run_path(poc.__file__, run_name="__main__")
        self.assertEqual(raised.exception.code, 130)
        self.assertIn("Interrupted.", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
