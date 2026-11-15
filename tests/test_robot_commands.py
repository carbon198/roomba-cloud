import contextlib
import io
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, Mock, patch

from roomba_cloud.mqtt_server import observation_packet, PacketReader
from roomba_cloud.robot_commands import (
    CommandError,
    CommandRegistry,
    CommandServer,
    CommandSession,
    request_command,
)


class FakeRobot:
    def __init__(self):
        self.session = None
        self.payloads = []
        self.reason = 0

    def sendall(self, wire):
        pos = 1
        while wire[pos] & 128:
            pos += 1
        reader = PacketReader(wire[pos + 1 :])
        topic = reader.text()
        identifier = reader.identifier()
        if self.session.version == 5:
            reader.properties()
        self.payloads.append((topic, json.loads(reader.body[reader.pos :])))
        self.session.acknowledge(identifier, self.reason)


class CommandTests(unittest.TestCase):
    def test_command_session_validation_acknowledgement_and_socket_proxy(self):
        conn = Mock()
        conn.fileno.return_value = 10
        session = CommandSession(conn, 4, "a" * 32)
        self.assertEqual(session.fileno(), 10)
        session.acknowledge(1, 0)
        self.assertFalse(session.ack.is_set())
        session.pending = 1
        session.acknowledge(1, 0)
        self.assertTrue(session.ack.is_set())

        for args in (
            ("reset", None, None),
            ("start", {}, None),
            ("start", None, 3),
            ("pause", None, 2),
        ):
            with self.subTest(args=args), self.assertRaises(CommandError):
                session.execute(args[0], args[1], args[2])

        session.pending = None
        session.subscriptions.add(session.topic)
        with (
            patch.object(
                session.ack,
                "wait",
                side_effect=lambda _: (session.close() or True),
            ),
            self.assertRaisesRegex(CommandError, "Robot disconnected"),
        ):
            session.locate()
        with self.assertRaisesRegex(CommandError, "only valid for start"):
            session.execute("pause", room_target={})

    def test_registry_replacement_and_reported_setting_command(self):
        robot_id = "f" * 32
        previous = Mock()
        current = Mock()
        registry = CommandRegistry()
        registry.register(robot_id, previous)
        registry.register(robot_id, current)
        previous.close.assert_called_once()
        registry.remove(robot_id, previous)
        self.assertIs(registry.sessions[robot_id], current)

        from roomba_cloud.shadow_service import ShadowService

        with tempfile.TemporaryDirectory() as directory:
            service = ShadowService(Path(directory) / "shadow.db")
            service.respond(
                f"$aws/things/{robot_id}/shadow/name/rw-settings/update",
                b'{"state":{"reported":{"childLock":true}}}',
                robot_id,
            )
            service.respond(
                f"$aws/things/{robot_id}/shadow/update",
                b'{"state":{"reported":{"cap":{"oMode":2}}}}',
                robot_id,
            )
            robot = FakeRobot()
            session = CommandSession(robot, 5, robot_id)
            robot.session = session
            session.subscriptions.add(session.settings_topic)
            session.subscriptions.add(session.topic)
            settings_registry = CommandRegistry(service.path)
            settings_registry.register(robot_id, session)
            result = settings_registry.execute(
                {
                    "robot_id": robot_id,
                    "command": "set_setting",
                    "key": "child_lock",
                    "value": False,
                }
            )
            self.assertEqual(result, {"delivery": "acknowledged", "execution": "unconfirmed"})
            self.assertEqual(robot.payloads[0][0], session.settings_topic)
            settings_registry.execute(
                {"robot_id": robot_id, "command": "start", "operating_mode": 2}
            )
            self.assertEqual(robot.payloads[1][1]["params"], {"operatingMode": 2})

    def test_request_command_sends_valid_mode_and_setting_fields(self):
        for command, options, expected in (
            ("start", {"operating_mode": 2}, {"operating_mode": 2}),
            (
                "set_setting",
                {"setting": {"key": "child_lock", "value": True}},
                {"key": "child_lock", "value": True},
            ),
        ):
            conn = MagicMock()
            conn.makefile.return_value = io.BytesIO(
                b'{"delivery":"acknowledged","execution":"unconfirmed"}\n'
            )
            with patch("roomba_cloud.robot_commands.socket.socket") as socket_factory:
                socket_factory.return_value.__enter__.return_value = conn
                request_command("unused.sock", "a" * 32, command, **options)
            request = json.loads(conn.sendall.call_args.args[0])
            self.assertEqual({key: request[key] for key in expected}, expected)

    def test_command_server_rejects_unsafe_socket_paths_and_preserves_replacements(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o755)
            with self.assertRaisesRegex(ValueError, "directory must be private"):
                CommandServer(root / "commands.sock", CommandRegistry())
            root.chmod(0o700)

            path = root / "unsafe.sock"
            active = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            active.bind(str(path))
            path.chmod(0o644)
            active.close()
            with self.assertRaisesRegex(ValueError, "private owned socket"):
                CommandServer(path, CommandRegistry())
            path.unlink()

            path = root / "replacement.sock"
            server = CommandServer(path, CommandRegistry())
            replacement = root / "replacement"
            replacement.write_text("replacement")
            path.unlink()
            path.symlink_to(replacement)
            server.server_close()
            self.assertTrue(path.is_symlink())

    def test_command_server_propagates_unexpected_stale_socket_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stale.sock"
            stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            stale.bind(str(path))
            path.chmod(0o600)
            stale.close()
            probe = MagicMock()
            probe.__enter__.return_value = probe
            probe.connect.side_effect = PermissionError("not a refused socket")
            with (
                patch("roomba_cloud.robot_commands.socket.socket", return_value=probe),
                self.assertRaises(PermissionError),
            ):
                CommandServer(path, CommandRegistry())

    def test_request_command_rejects_invalid_optional_parameters_and_framing(self):
        for command, options in (
            ("unknown", {}),
            ("find", {"edit": {}}),
            ("find", {"duration": 1}),
            ("find", {"operating_mode": 2}),
            ("find", {"setting": {"key": "x", "value": 1}}),
            ("find", {"segment_ids": ["one"]}),
            ("live_map", {"duration": True}),
            ("set_setting", {"setting": []}),
            ("start", {"operating_mode": True}),
        ):
            with self.subTest(command=command, options=options):
                with self.assertRaises(CommandError):
                    request_command("unused.sock", "a" * 32, command, **options)

        for raw in (b'{"delivery":"acknowledged"}', b"x" * 1025 + b"\n"):
            conn = MagicMock()
            conn.makefile.return_value = io.BytesIO(raw)
            with (
                patch("roomba_cloud.robot_commands.socket.socket") as socket_factory,
                self.assertRaisesRegex(CommandError, "Invalid command service response"),
            ):
                socket_factory.return_value.__enter__.return_value = conn
                request_command("unused.sock", "a" * 32)

    def test_registry_rejects_disabled_settings_edits_rooms_and_mapping(self):
        robot_id = "a" * 32
        session = CommandSession(Mock(), 5, robot_id)
        session.subscriptions.add(session.topic)
        registry = CommandRegistry()
        registry.register(robot_id, session)
        requests = (
            {"robot_id": robot_id, "command": "set_setting", "key": "child_lock", "value": True},
            {"robot_id": robot_id, "command": "room_edit", "edit": {}},
            {"robot_id": robot_id, "command": "clean_segments", "segment_ids": []},
            {"robot_id": robot_id, "command": "train"},
        )
        for request in requests:
            with self.subTest(command=request["command"]), self.assertRaises(CommandError):
                registry.execute(request)

    def test_command_server_returns_errors_for_invalid_framing_and_json(self):
        with tempfile.TemporaryDirectory() as directory:
            with CommandServer(Path(directory) / "commands.sock", CommandRegistry()) as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    for payload, expected in (
                        (b"x" * 65537 + b"\n", "Invalid command framing"),
                        (b"not-json\n", "Command transport failed"),
                    ):
                        with self.subTest(expected=expected), socket.socket(
                            socket.AF_UNIX, socket.SOCK_STREAM
                        ) as conn:
                            conn.settimeout(2)
                            conn.connect(str(server.path))
                            conn.sendall(payload)
                            with conn.makefile("rb") as response:
                                self.assertEqual(
                                    json.loads(response.readline()), {"error": expected}
                                )
                finally:
                    server.shutdown()
                    thread.join()

    def test_mapping_run_requires_no_mission_and_live_subscription(self):
        from roomba_cloud.shadow_service import ShadowService

        robot = FakeRobot()
        session = CommandSession(robot, 5, "a" * 32)
        robot.session = session
        request = {"robot_id": "a" * 32, "command": "train"}
        with tempfile.TemporaryDirectory() as directory:
            service = ShadowService(Path(directory) / "state.db")
            registry = CommandRegistry(service.path)
            with self.assertRaises(CommandError):
                registry.execute(request)
            registry.register("a" * 32, session)
            session.subscriptions.add(session.topic)
            topic = "$aws/things/" + "a" * 32 + "/shadow/name/ro-currentstate/update"
            for mission in (
                {},
                {"phase": "run", "cycle": "clean"},
                {"phase": "pause", "cycle": "clean"},
                {"phase": "pause", "cycle": "none"},
                {"phase": "run", "cycle": "train"},
                {"phase": "charge", "cycle": "clean"},
                {"phase": "pause", "cycle": "train"},
            ):
                service.respond(
                    topic,
                    json.dumps({"state": {"reported": {"cleanMissionStatus": mission}}}).encode(),
                    "a" * 32,
                )
                with self.assertRaises(CommandError):
                    registry.execute(request)
            self.assertEqual(robot.payloads, [])
            service.respond(
                topic,
                json.dumps(
                    {
                        "state": {
                            "reported": {"cleanMissionStatus": {"phase": "charge", "cycle": "none"}}
                        }
                    }
                ).encode(),
                "a" * 32,
            )
            with patch("roomba_cloud.robot_commands.time.time", return_value=123):
                self.assertEqual(
                    registry.execute(request),
                    {"delivery": "acknowledged", "execution": "unconfirmed"},
                )
            self.assertEqual(
                robot.payloads,
                [(session.topic, {"command": "train", "time": 123, "initiator": "localApp"})],
            )
            with self.assertRaises(CommandError):
                registry.execute({**request, "operating_mode": 2})
            session.close()
            with self.assertRaises(CommandError):
                registry.execute(request)
            self.assertEqual(len(robot.payloads), 1)

    def test_ipc_response_shapes_are_validated_before_use(self):
        from unittest.mock import MagicMock

        for command, response in (
            ("find", None),
            ("find", 12),
            ("find", []),
            ("find", "error"),
            ("find", {"error": []}),
            ("find", {"error": "failed", "extra": True}),
            ("find", {"delivery": "acknowledged"}),
            ("room_edit", {"transaction_id": True}),
            ("room_edit", {"transaction_id": 0}),
            ("room_edit", {"transaction_id": 1, "extra": 2}),
        ):
            with self.subTest(command=command, response=response):
                conn = MagicMock()
                conn.makefile.return_value = io.BytesIO(json.dumps(response).encode() + b"\n")
                with patch("roomba_cloud.robot_commands.socket.socket") as socket_factory:
                    socket_factory.return_value.__enter__.return_value = conn
                    with self.assertRaisesRegex(CommandError, "Invalid"):
                        request_command("unused.sock", "a" * 32, command)

    def test_exact_live_map_telemetry_request_and_validation_over_ipc(self):
        robot = FakeRobot()
        session = CommandSession(robot, 5, "a" * 32)
        robot.session = session
        registry = CommandRegistry()
        registry.register("a" * 32, session)
        request = {"robot_id": "a" * 32, "command": "live_map", "duration": 60}
        with self.assertRaises(CommandError):
            registry.execute(request)
        self.assertFalse(robot.payloads)
        session.subscriptions.add(session.live_map_topic)
        with tempfile.TemporaryDirectory() as directory:
            with CommandServer(Path(directory) / "commands.sock", registry) as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    request_command(server.path, "a" * 32, "live_map", duration=60)
                finally:
                    server.shutdown()
                    thread.join()
        self.assertEqual(
            robot.payloads,
            [
                (
                    "v011-irbthbu/things/" + "a" * 32 + "/livemap/cmd",
                    {"operation": "start", "start": {"duration": 60}},
                )
            ],
        )
        for duration in (True, None, 0, 301, 1.5, "60"):
            with self.assertRaises(CommandError):
                registry.execute({**request, "duration": duration})
            with self.assertRaises(CommandError):
                request_command("unused", "a" * 32, "live_map", duration=duration)
        for extra in ({"operating_mode": 6}, {"segment_ids": ["one"]}, {"operation": "reset"}):
            with self.assertRaises(CommandError):
                registry.execute({**request, **extra})
        with self.assertRaises(CommandError):
            request_command("unused", "a" * 32, "start", duration=60)
        self.assertEqual(len(robot.payloads), 1)

    def test_delivery_and_disconnected_rejection_no_replay(self):
        robot = FakeRobot()
        session = CommandSession(robot, 5, "a" * 32)
        robot.session = session
        with self.assertRaises(CommandError):
            session.locate()
        session.subscriptions.add(session.topic)
        registry = CommandRegistry()
        registry.register("a" * 32, session)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "commands.sock"
            with CommandServer(path, registry) as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                    self.assertEqual(
                        request_command(path, "a" * 32),
                        {"delivery": "acknowledged", "execution": "unconfirmed"},
                    )
                    self.assertEqual(robot.payloads[0][1]["command"], "find")
                    self.assertEqual(robot.payloads[0][1]["initiator"], "localApp")
                    self.assertIs(type(robot.payloads[0][1]["time"]), int)
                    registry.remove("a" * 32, session)
                    with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(CommandError):
                        request_command(path, "a" * 32)
                    self.assertEqual(len(robot.payloads), 1)
                finally:
                    server.shutdown()
                    thread.join()
            self.assertFalse(path.exists())

    def test_puback_versions_and_negative_ack(self):
        for version, body in ((4, b"\0\x01"), (5, b"\0\x01\0\0")):
            event, fields, _, _ = observation_packet(0x40, body, version)
            self.assertEqual(event, "mqtt_puback")
            self.assertEqual(fields, {"packet_id": 1, "reason_code": 0})
        robot = FakeRobot()
        session = CommandSession(robot, 5, "a" * 32)
        robot.session = session
        robot.reason = 128
        session.subscriptions.add(session.topic)
        with self.assertRaisesRegex(CommandError, "rejected"):
            session.locate()
        for command in ("start", "reset", "find"):
            with self.assertRaises(CommandError):
                CommandRegistry().execute({"robot_id": "a" * 32, "command": command})

    def test_timeout_and_concurrent_command_never_retry(self):
        robot = FakeRobot()
        session = CommandSession(robot, 5, "a" * 32)
        robot.session = session
        session.subscriptions.add(session.topic)
        with patch.object(session.ack, "wait", return_value=False):
            with self.assertRaisesRegex(CommandError, "uncertain"):
                session.locate()
        self.assertEqual(len(robot.payloads), 1)

    def test_all_mission_verbs_and_allowlist(self):
        robot = FakeRobot()
        session = CommandSession(robot, 5, "a" * 32)
        robot.session = session
        session.subscriptions.add(session.topic)
        registry = CommandRegistry()
        registry.register("a" * 32, session)
        for command in ("start", "pause", "stop", "dock", "find"):
            registry.execute({"robot_id": "a" * 32, "command": command})
            self.assertEqual(robot.payloads[-1][1]["command"], command)
        for command in ("reset", "resume", "washpad", "start;reset", {}):
            with self.assertRaises(CommandError):
                registry.execute({"robot_id": "a" * 32, "command": command})
        self.assertEqual(len(robot.payloads), 5)
        session.command_lock.acquire()
        try:
            with self.assertRaisesRegex(CommandError, "in progress"):
                session.locate()
        finally:
            session.command_lock.release()
        self.assertEqual(len(robot.payloads), 5)
