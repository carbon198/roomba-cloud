import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import contextlib
import io

from roomba_cloud.shadow_service import ShadowService
from roomba_cloud.mqtt_server import observe, topic_matches
from roomba_cloud.robot_mqtt import mqtt_string


class ShadowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "shadows.db"
        self.service = ShadowService(self.path)
        self.base = "$aws/things/test/shadow/name/ro-currentstate/"

    def request(self, action, data):
        return self.service.respond(self.base + action, json.dumps(data).encode(), "test")

    def test_missing_get_echoes_token_and_does_not_create_state(self):
        topic, result = self.request("get", {"clientToken": "private-token"})
        self.assertEqual(topic, self.base + "get/rejected")
        self.assertEqual(result["code"], 404)
        self.assertEqual(result["clientToken"], "private-token")
        self.assertNotIn("state", result)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_recursive_merge_deletion_arrays_and_persistence(self):
        self.request(
            "update",
            {"state": {"reported": {"batPct": 70, "dock": {"a": 1, "b": 2}, "maps": [1, 2]}}},
        )
        _, result = self.request(
            "update",
            {
                "clientToken": "token",
                "version": 1,
                "state": {"reported": {"dock": {"a": None, "c": True}, "maps": [3]}},
            },
        )
        self.assertEqual(result["version"], 2)
        self.assertEqual(result["clientToken"], "token")
        self.service = ShadowService(self.path)
        _, result = self.request("get", {"clientToken": "other"})
        self.assertEqual(
            result["state"]["reported"], {"batPct": 70, "dock": {"b": 2, "c": True}, "maps": [3]}
        )
        self.assertNotIn("a", result["metadata"]["reported"]["dock"])
        self.assertIn("timestamp", result["metadata"]["reported"]["batPct"])
        self.assertEqual(result["clientToken"], "other")

    def test_invalid_and_desired_updates_do_not_change_state(self):
        for data in (
            [],
            {"state": {"desired": {"start": True}}},
            {"state": {"reported": []}},
            {"clientToken": 4},
            {"state": {"reported": {}}, "version": True},
        ):
            with self.subTest(data=data):
                _, response = self.request("update", data)
                self.assertEqual(response["code"], 400)
        for payload in (
            b"not json",
            b'{"state":{"reported":{"x":NaN}}}',
            b'{"state":{"reported":{"x":1e999}}}',
            b"\xff",
        ):
            self.assertEqual(
                self.service.respond(self.base + "update", payload, "test")[1]["code"], 400
            )
        self.assertEqual(self.request("get", {})[1]["code"], 404)

    def test_version_conflict_and_client_isolation(self):
        self.request("update", {"state": {"reported": {"batPct": 70}}})
        self.assertEqual(
            self.request("update", {"version": 0, "state": {"reported": {"batPct": 10}}})[1][
                "code"
            ],
            409,
        )
        self.assertEqual(self.request("get", {})[1]["state"]["reported"]["batPct"], 70)
        self.assertIsNone(self.service.respond(self.base + "get", b"{}", "different"))
        self.assertIsNone(self.service.respond("custom/cmd", b"{}", "test"))

    def test_settings_reports_clear_desired_without_persisting_commands(self):
        from roomba_cloud.robot_settings import SETTINGS, configuration

        robot_id = "a" * 32
        base = f"$aws/things/{robot_id}/shadow/name/rw-settings/"
        initial = {
            wire: False if expected is bool else min(options)
            for wire, _, _, expected, options in SETTINGS.values()
        }
        for topic, reported in (
            (f"$aws/things/{robot_id}/shadow/update", {"cap": {"autoevac": 3, "multiPass": 1}}),
            (base + "update", initial),
        ):
            self.service.respond(
                topic, json.dumps({"state": {"reported": reported}}).encode(), robot_id
            )
        for key, (wire, _, _, expected, options) in SETTINGS.items():
            with self.subTest(setting=key):
                value = True if expected is bool else sorted(options)[1]
                delta = self.service.setting_delta(robot_id, key, value)
                self.assertEqual(delta["state"], {wire: value})
                topic, result = self.service.respond(
                    base + "update",
                    json.dumps(
                        {
                            "state": {"reported": {wire: value}, "desired": {wire: None}},
                        }
                    ).encode(),
                    robot_id,
                )
                self.assertEqual(topic, base + "update/accepted")
                self.assertEqual(result["version"], delta["version"] + 1)
                self.assertEqual(result["state"]["reported"], {wire: value})
                self.assertIn(wire, result["metadata"]["reported"])
                values = {item["key"]: item["value"] for item in configuration(self.path, robot_id)}
                self.assertEqual(values[key], value)
                stored = self.service.respond(base + "get", b"{}", robot_id)[1]
                self.assertNotIn("desired", stored["state"])
                self.assertNotIn("desired", stored["metadata"])

    def test_desired_deletions_and_rejection_of_desired_values_are_atomic(self):
        for desired in (None, {}, {"childLock": None}, {"nested": {"field": None}}):
            with self.subTest(desired=desired):
                topic, result = self.request(
                    "update", {"state": {"reported": {"childLock": True}, "desired": desired}}
                )
                self.assertTrue(topic.endswith("/accepted"))
                self.assertEqual(result["state"]["desired"], desired)
        before = self.request("get", {})[1]
        for desired in (True, False, 0, "", [], {"childLock": False}, {"nested": {"field": 1}}):
            with self.subTest(desired=desired):
                _, result = self.request(
                    "update", {"state": {"reported": {"childLock": False}, "desired": desired}}
                )
                self.assertEqual(result["code"], 400)
                stored = self.request("get", {})[1]
                self.assertEqual(stored["version"], before["version"])
                self.assertEqual(stored["state"], before["state"])
        self.assertEqual(
            self.request("update", {"state": {"reported": {}, "unexpected": None}})[1]["code"], 400
        )

    def test_private_existing_file_and_symlink(self):
        public = Path(self.directory.name) / "public"
        public.write_text("")
        os.chmod(public, 0o644)
        with self.assertRaises(ValueError):
            ShadowService(public)
        link = Path(self.directory.name) / "link"
        link.symlink_to(self.path)
        with self.assertRaises(ValueError):
            ShadowService(link)

    def test_request_document_and_count_limits(self):
        self.assertEqual(
            self.service.respond(self.base + "get", b"x" * (256 * 1024 + 1), "test")[1]["code"], 413
        )
        state = {str(i): "x" for i in range(6000)}
        self.assertEqual(self.request("update", {"state": {"reported": state}})[1]["code"], 413)
        self.assertEqual(self.request("get", {})[1]["code"], 404)
        for i in range(100):
            topic = f"$aws/things/test/shadow/name/n{i}/update"
            reply = self.service.respond(topic, b'{"state":{"reported":{}}}', "test")
            self.assertTrue(reply[0].endswith("/accepted"))
        self.assertEqual(self.request("update", {"state": {"reported": {}}})[1]["code"], 413)

    def test_unnamed_shadow_and_no_persisted_client_token(self):
        base = "$aws/things/test/shadow/"
        self.service.respond(
            base + "update", b'{"clientToken":"private","state":{"reported":{"x":1}}}', "test"
        )
        topic, result = self.service.respond(base + "get", b"{}", "test")
        self.assertEqual(topic, base + "get/accepted")
        self.assertNotIn("clientToken", result)

    def test_unsubscribed_response_not_delivered_but_state_stored(self):
        payload = b'{"state":{"reported":{"batPct":55},"desired":{"batPct":null}}}'
        body = mqtt_string(self.base + "update") + b"\0" + payload
        conn = Mock()
        output = io.StringIO()
        with (
            patch("roomba_cloud.mqtt_server.read_frame", side_effect=[(0x30, body), (0xE0, b"")]),
            contextlib.redirect_stdout(output),
        ):
            observe(
                conn,
                "127.0.0.1",
                {"client_id": "test", "protocol_version": 5, "keepalive_seconds": 30},
                bytearray(),
                5,
                shadows=self.service,
            )
        conn.sendall.assert_not_called()
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        reply = next(r for r in records if r["event"] == "mqtt_shadow_response")
        self.assertFalse(reply["delivered"])
        self.assertEqual(reply["code"], 200)
        self.assertNotIn("message", reply)
        self.assertEqual(self.request("get", {})[1]["state"]["reported"]["batPct"], 55)
        self.assertNotIn("batPct", output.getvalue())

    def test_shadow_rejection_logs_reason_without_request_payload(self):
        payload = b'{"state":{"reported":{"private-field":55},"desired":{"private-field":true}}}'
        body = mqtt_string(self.base + "update") + b"\0" + payload
        output = io.StringIO()
        with (
            patch("roomba_cloud.mqtt_server.read_frame", side_effect=[(0x30, body), (0xE0, b"")]),
            contextlib.redirect_stdout(output),
        ):
            observe(
                Mock(),
                "127.0.0.1",
                {"client_id": "test", "protocol_version": 5, "keepalive_seconds": 30},
                bytearray(),
                5,
                shadows=self.service,
            )
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        reply = next(r for r in records if r["event"] == "mqtt_shadow_response")
        self.assertEqual(reply["code"], 400)
        self.assertEqual(reply["message"], "Only desired deletions are supported")
        self.assertNotIn("private-field", output.getvalue())
        self.assertEqual(self.request("get", {})[1]["code"], 404)

    def test_topic_matching(self):
        for subscription in (self.base + "get/accepted", "$aws/things/+/shadow/#", "$aws/#"):
            self.assertTrue(topic_matches(subscription, self.base + "get/accepted"))
        self.assertFalse(topic_matches("#", self.base + "get/accepted"))
        self.assertFalse(topic_matches("$aws/things/other/#", self.base + "get/accepted"))
        self.assertTrue(topic_matches("a/#", "a"))
