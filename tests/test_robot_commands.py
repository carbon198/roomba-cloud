import contextlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

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
