"""Exercise customer HTTP routing against synthetic storage and command IPC."""

import contextlib
import http.client
from http.server import ThreadingHTTPServer
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import roomba_cloud.cloud_http as cloud_http
from roomba_cloud.robot_commands import CommandError, CommandRegistry, CommandServer, CommandSession
from roomba_cloud.maps.room_edits import editor_maps, initialize
from test_robot_commands import FakeRobot
from test_room_catalog import populate, RoomCatalogTests


class CustomerRouteTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.output = self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.shadows, self.uploads, self.robot_id = populate(directory)
        self.map_id = self.robot_id + "-100"
        fixture = RoomCatalogTests()
        fixture.directory = SimpleNamespace(name=directory)
        fixture.uploads = self.uploads
        fixture.robot_id = self.robot_id
        fixture.mission_archive(map_id=self.map_id)
        self.report(
            {
                "p2maps": [{"p2map_id": self.map_id, "p2mapv_id": "1.0"}],
                "cleanMissionStatus": {"cycle": "none", "phase": "charge"},
            }
        )
        initialize(self.shadows.path)
        self.fake = FakeRobot()
        self.session = CommandSession(self.fake, 5, self.robot_id)
        self.fake.session = self.session
        self.session.subscriptions.update(
            (self.session.topic, self.session.edit_topic, self.session.live_map_topic)
        )
        registry = CommandRegistry(self.shadows.path, self.uploads.path)
        registry.register(self.robot_id, self.session)
        self.commands = self.stack.enter_context(
            CommandServer(Path(directory) / "commands.sock", registry)
        )
        self.start(self.commands)
        self.server = self.stack.enter_context(
            ThreadingHTTPServer(("127.0.0.1", 0), cloud_http.cloud_handler())
        )
        self.server.customer_auth = cloud_http.CustomerAuth(b"a" * 64)
        self.server.customer_shadow_db = self.shadows.path
        self.server.command_socket = self.commands.path
        self.server.uploads = self.uploads
        self.server.discovery = b"{}"
        self.server.registration_ok = True
        self.start(self.server)
        self.base = f"/v1/customer/robots/{self.robot_id}"

    def start(self, server):
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        self.stack.callback(thread.join, 5)
        self.stack.callback(server.shutdown)

    def report(self, reported):
        self.shadows.respond(
            f"$aws/things/{self.robot_id}/shadow/name/ro-currentstate/update",
            json.dumps({"state": {"reported": reported}}).encode(),
            self.robot_id,
        )

    def request(self, suffix, method="GET", body=None, *, auth=True, headers=()):
        with contextlib.closing(
            http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        ) as conn:
            conn.putrequest(method, self.base + suffix)
            if auth:
                conn.putheader("Authorization", "Bearer " + "a" * 64)
            if body is not None:
                body = body.encode() if isinstance(body, str) else body
                if not any(key.lower() == "content-length" for key, _ in headers):
                    conn.putheader("Content-Length", str(len(body)))
            for key, value in headers:
                conn.putheader(key, value)
            conn.endheaders(body)
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()

    def rename(self, name="Kitchen"):
        model = editor_maps(self.shadows.path, self.uploads.path, self.robot_id)["maps"][0]
        return {
            "map_id": model["id"],
            "version": model["version"],
            "revision": model["revision"],
            "room_id": "1",
            "operation": "rename",
            "name": name,
        }

    def test_every_customer_surface_requires_authentication(self):
        for route, method, body in (
            ("/room-editor", "GET", None),
            ("/room-edits", "POST", "{}"),
            ("/room-edits/1", "GET", None),
            ("/telemetry", "GET", None),
            ("/events", "GET", None),
            ("/locate", "POST", None),
            ("/commands/start", "POST", None),
            ("/commands/train", "POST", None),
            ("/settings/child_lock", "POST", '{"value":true}'),
            (f"/maps/{self.map_id}/1.0/{'a' * 64}/live.png", "GET", None),
        ):
            with self.subTest(route=route):
                status, headers, _ = self.request(route, method, body, auth=False)
                self.assertEqual(status, 401)
                self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertFalse(self.fake.payloads)

    def test_mapping_run_route_is_post_only_and_docked_only(self):
        self.assertEqual(self.request("/commands/train")[0], 405)
        self.assertEqual(self.request("/commands/train", "POST", "{}")[0], 400)
        for phase, cycle in (("idle", "none"), ("charge", "clean"), ("run", "train")):
            self.report({"cleanMissionStatus": {"phase": phase, "cycle": cycle}})
            self.assertEqual(self.request("/commands/train", "POST")[0], 503)
        self.assertFalse(self.fake.payloads)
        self.report({"cleanMissionStatus": {"phase": "charge", "cycle": "none"}})
        status, _, body = self.request("/commands/train", "POST")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"delivery": "acknowledged", "execution": "unconfirmed"})
        self.assertEqual(len(self.fake.payloads), 1)
        self.assertEqual(self.fake.payloads[0][1]["command"], "train")

    def test_editor_and_rename_round_trip_through_real_ipc(self):
        status, headers, body = self.request("/room-editor")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertTrue(json.loads(body)["map_controls"]["allowed"])
        status, _, body = self.request(
            "/room-edits", "POST", json.dumps(self.rename("  廚房 & Dining  "))
        )
        self.assertEqual(status, 200)
        transaction = json.loads(body)["transaction_id"]
        self.assertEqual(
            self.fake.payloads[0][1]["edit_cmd"]["params"]["room_name"], "廚房 & Dining"
        )
        status, _, body = self.request(f"/room-edits/{transaction}")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "pending")
        status, _, body = self.request("/room-editor")
        self.assertTrue(json.loads(body)["maps"][0]["awaiting_upload"])
        self.assertEqual(self.request("/room-edits/9999")[0], 400)
        self.assertNotIn("a" * 64, self.output.getvalue())

    def test_invalid_names_and_missions_never_reach_the_robot(self):
        for name in ("", " " * 4, "x" * 65, "Kitchen\n", "a\u200bb", "\ud800"):
            with patch("roomba_cloud.robot_commands.request_command") as send:
                self.assertEqual(
                    self.request("/room-edits", "POST", json.dumps(self.rename(name)))[0], 400
                )
                send.assert_not_called()
        edit = self.rename()
        for mission in (
            {"cycle": "clean", "phase": "run"},
            {"cycle": "clean", "phase": "pause"},
            {},
        ):
            self.report({"cleanMissionStatus": mission})
            self.assertEqual(self.request("/room-edits", "POST", json.dumps(edit))[0], 503)
        self.assertFalse(self.fake.payloads)
        with contextlib.closing(sqlite3.connect(self.shadows.path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM room_edits").fetchone()[0], 0)

    def test_edit_body_framing_json_and_methods_are_checked_before_ipc(self):
        for body, headers in (
            ("{}", (("Content-Length", "0"),)),
            ("{}", (("Content-Length", "8193"),)),
            ("{}", (("Content-Length", "2"), ("Content-Length", "2"))),
            ("{}", (("Transfer-Encoding", "chunked"),)),
            ("{", ()),
            ("[" * 1500, ()),
        ):
            with self.subTest(headers=headers, body=body[:10]):
                self.assertEqual(self.request("/room-edits", "POST", body, headers=headers)[0], 400)
        for route, method in (
            ("/room-edits", "GET"),
            ("/room-edits/1", "POST"),
            ("/room-editor", "POST"),
            ("/telemetry", "POST"),
            ("/commands/start", "GET"),
        ):
            self.assertEqual(self.request(route, method)[0], 405)
        self.assertFalse(self.fake.payloads)

    def test_editor_and_edit_dependencies_fail_explicitly(self):
        self.server.command_socket = None
        self.assertEqual(self.request("/room-edits", "POST", "{}")[0], 503)
        self.server.uploads = None
        self.assertEqual(self.request("/room-editor")[0], 503)
        self.server.uploads = self.uploads
        with patch(
            "roomba_cloud.maps.room_edits.editor_maps", side_effect=ValueError("private details")
        ):
            status, _, body = self.request("/room-editor")
            self.assertEqual(status, 500)
            self.assertNotIn(b"private details", body)
        self.server.command_socket = (
            Path(self.stack.enter_context(tempfile.TemporaryDirectory())) / "missing.sock"
        )
        status, _, body = self.request("/room-edits", "POST", json.dumps(self.rename()))
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body)["error"], "Room edit service unavailable")
        self.assertNotIn(str(self.server.command_socket), self.output.getvalue())

    def test_commands_validate_payloads_and_forward_verified_shapes(self):
        for route, body, wire in (
            ("/locate", None, "find"),
            ("/commands/start", None, "start"),
            ("/commands/pause", None, "pause"),
            ("/commands/stop", None, "stop"),
            ("/commands/dock", None, "dock"),
        ):
            status, headers, response = self.request(route, "POST", body)
            self.assertEqual(status, 200)
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertEqual(
                json.loads(response), {"delivery": "acknowledged", "execution": "unconfirmed"}
            )
            self.assertEqual(self.fake.payloads[-1][1]["command"], wire)
        self.assertEqual(self.request("/commands/live_map", "POST")[0], 200)
        self.assertEqual(
            self.fake.payloads[-1][1], {"operation": "start", "start": {"duration": 60}}
        )
        sent = len(self.fake.payloads)
        for route, body in (
            ("/locate", "{}"),
            ("/commands/live_map", "{}"),
            ("/commands/start", "[]"),
            ("/commands/start", '{"extra":1}'),
            ("/commands/start", '{"operating_mode":true}'),
            ("/commands/clean_segments", '{"segment_ids":[]}'),
            ("/commands/clean_segments", '{"segment_ids":["x","x"]}'),
            ("/settings/unknown", '{"value":true}'),
            ("/settings/child_lock", '{"value":"true"}'),
        ):
            with self.subTest(route=route, body=body):
                self.assertEqual(self.request(route, "POST", body)[0], 400)
        self.assertEqual(len(self.fake.payloads), sent)
        with patch(
            "roomba_cloud.robot_commands.request_command", side_effect=CommandError("disconnected")
        ):
            self.assertEqual(self.request("/locate", "POST")[0], 503)

    def test_command_targets_modes_and_settings_reach_ipc(self):
        with patch(
            "roomba_cloud.robot_commands.request_command",
            return_value={"delivery": "acknowledged", "execution": "unconfirmed"},
        ) as send:
            self.assertEqual(
                self.request("/commands/start", "POST", '{"operating_mode":6}')[0], 200
            )
            self.assertEqual(send.call_args.kwargs["operating_mode"], 6)
            self.assertEqual(
                self.request(
                    "/commands/clean_segments",
                    "POST",
                    '{"segment_ids":["map:1"],"operating_mode":2}',
                )[0],
                200,
            )
            self.assertEqual(send.call_args.args[3], ["map:1"])
            self.assertEqual(send.call_args.kwargs["operating_mode"], 2)
            with patch(
                "roomba_cloud.robot_settings.setting_patch", return_value={"childLock": True}
            ):
                self.assertEqual(
                    self.request("/settings/child_lock", "POST", '{"value":true}')[0], 200
                )
                self.assertEqual(
                    send.call_args.kwargs["setting"], {"key": "child_lock", "value": True}
                )
            with patch(
                "roomba_cloud.robot_settings.setting_patch", side_effect=ValueError("unsupported")
            ):
                self.assertEqual(
                    self.request("/settings/child_lock", "POST", '{"value":true}')[0], 400
                )
            with patch(
                "roomba_cloud.robot_settings.setting_patch", side_effect=sqlite3.OperationalError()
            ):
                self.assertEqual(
                    self.request("/settings/child_lock", "POST", '{"value":true}')[0], 503
                )

    def test_telemetry_and_images_return_data_not_found_and_redacted_errors(self):
        status, headers, body = self.request("/telemetry")
        self.assertEqual(status, 200)
        document = json.loads(body)
        self.assertEqual(document["schema_version"], 1)
        self.assertEqual(document["map_inventory"], [self.map_id])
        self.assertEqual(headers["Cache-Control"], "no-store")
        model = editor_maps(self.shadows.path, self.uploads.path, self.robot_id)["maps"][0]
        route = f"/maps/{self.map_id}/{model['version']}/{model['revision']}/room.png"
        status, headers, body = self.request(route)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertTrue(body.startswith(b"\x89PNG"))
        self.assertEqual(self.request(route.replace(model["revision"], "0" * 64))[0], 404)
        self.assertEqual(self.request(route, "POST")[0], 405)
        with patch(
            "roomba_cloud.maps.customer_maps.image", side_effect=ValueError("private details")
        ):
            self.assertEqual(self.request(route)[0], 500)
        with patch("roomba_cloud.customer_telemetry.snapshot", return_value=None):
            self.assertEqual(self.request("/telemetry")[0], 404)
        with patch(
            "roomba_cloud.customer_telemetry.snapshot", side_effect=ValueError("private details")
        ):
            self.assertEqual(self.request("/telemetry")[0], 500)
        self.assertNotIn("private details", self.output.getvalue())
        self.server.customer_shadow_db = None
        for target in ("/telemetry", route, "/room-editor"):
            self.assertEqual(self.request(target)[0], 503)

    def test_event_route_initial_snapshot_and_heartbeat(self):
        events = Mock(stopped=False)
        changed = Mock()
        events.subscribe.return_value = contextlib.nullcontext(changed)
        self.server.telemetry_events = events
        count = 0

        def wait(timeout):
            nonlocal count
            self.assertEqual(timeout, 15)
            count += 1
            if count == 2:
                events.stopped = True

        changed.wait.side_effect = wait
        status, headers, body = self.request("/events")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/event-stream")
        self.assertEqual(body.count(b"event: telemetry"), 1)
        self.assertIn(b": heartbeat", body)
        events.subscribe.assert_called_once_with(self.robot_id)
        self.assertEqual(self.request("/events")[0], 503)
        events.stopped = False
        with patch("roomba_cloud.customer_telemetry.snapshot", return_value=None):
            self.assertEqual(self.request("/events")[0], 404)
        with patch(
            "roomba_cloud.customer_telemetry.snapshot", side_effect=ValueError("private details")
        ):
            self.assertEqual(self.request("/events")[0], 500)
