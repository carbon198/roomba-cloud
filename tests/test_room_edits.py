"""Synthetic map editor snapshots and robot edit transactions."""

import contextlib
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from roomba_cloud.maps.room_edits import (
    editor_maps,
    edit_status,
    initialize,
    receive_result,
    require_idle,
    submit,
    validate_edit,
    wire_command,
    bootstrap_once,
    awaiting_upload,
    clear_uploaded_locks,
    require_room_crossing,
)
from test_room_catalog import populate


class RoomEditorTests(unittest.TestCase):
    def test_room_name_validation_and_normalization_before_publish(self):
        from unittest.mock import Mock
        from roomba_cloud.maps.room_edits import normalize_room_name

        model = self.mission_map(map_id=self.robot + "-100")
        edit = {
            "map_id": model["id"],
            "version": model["version"],
            "revision": model["revision"],
            "operation": "rename",
            "room_id": "1",
        }
        for name in (
            None,
            123,
            "",
            "   ",
            "a" * 65,
            "😀" * 65,
            "Kitchen\n",
            "\tKitchen",
            "a\x7fb",
            "a\x85b",
            "a\u200bb",
            "a\u202eb",
            "a\u2028b",
            "a\u2029b",
            "a\ud800b",
            "\ufeffKitchen",
        ):
            with self.subTest(name=repr(name)):
                session = Mock()
                with self.assertRaises(ValueError):
                    submit(
                        self.shadows.path,
                        self.uploads.path,
                        self.robot,
                        session,
                        edit | {"name": name},
                    )
                session.publish_once.assert_not_called()
                with self.assertRaises(ValueError):
                    wire_command(edit | {"name": name}, None)
        for name in (
            "  Kitchen  ",
            "Chambre d’amis",
            "廚房",
            "غرفة",
            "e\u0301",
            "😀" * 64,
            "<Kitchen & Dining>",
            "a" * 64,
        ):
            with self.subTest(name=name):
                self.assertEqual(normalize_room_name(name), name.strip())
                validate_edit(
                    self.shadows.path, self.uploads.path, self.robot, edit | {"name": name}
                )
                self.assertEqual(
                    wire_command(edit | {"name": name}, None)["params"]["room_name"], name.strip()
                )
        with contextlib.closing(sqlite3.connect(self.shadows.path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM room_edits").fetchone()[0], 0)

    def test_release_timestamp_migration_preserves_old_releases_and_active_locks(self):
        from pathlib import Path

        legacy = Path(self.directory.name) / "old-locks.db"
        with contextlib.closing(sqlite3.connect(legacy)) as db, db:
            db.execute(
                "CREATE TABLE room_edits (transaction_id INTEGER PRIMARY KEY,"
                "robot_id TEXT,created REAL,status TEXT,revision TEXT,map_id TEXT,"
                "archive_token TEXT,upload_received REAL,operation TEXT)"
            )
            for identifier, operation, stamp in (
                (1, "edit", 123),
                (2, "delete", 124),
                (3, "edit", None),
                (4, "delete", None),
            ):
                db.execute(
                    "INSERT INTO room_edits VALUES (?,?,0,'confirmed','rev',?,NULL,?,?)",
                    (identifier, self.robot, str(identifier), stamp, operation),
                )
        initialize(legacy)
        initialize(legacy)
        with contextlib.closing(sqlite3.connect(legacy)) as db, db:
            self.assertEqual(
                db.execute("SELECT released_at FROM room_edits ORDER BY transaction_id").fetchall(),
                [(123,), (124,), (None,), (None,)],
            )
            self.assertFalse(awaiting_upload(db, self.robot, "1"))
            self.assertFalse(awaiting_upload(db, self.robot, "2"))
            self.assertTrue(awaiting_upload(db, self.robot, "3"))
            self.assertTrue(awaiting_upload(db, self.robot, "4"))
            db.execute("UPDATE room_edits SET upload_received=999 WHERE transaction_id=3")
        initialize(legacy)
        with contextlib.closing(sqlite3.connect(legacy)) as db:
            self.assertTrue(awaiting_upload(db, self.robot, "3"))

    def test_delete_rejection_and_inventory_release_are_not_uploads(self):
        identifier = self.lock_map(self.robot + "-100")
        with contextlib.closing(sqlite3.connect(self.shadows.path)) as db, db:
            db.execute(
                "UPDATE room_edits SET operation='delete',status='pending' WHERE transaction_id=?",
                (identifier,),
            )
        receive_result(
            self.shadows.path,
            self.robot,
            json.dumps({"transaction_id": identifier, "status": "error"}).encode(),
        )
        with contextlib.closing(sqlite3.connect(self.shadows.path)) as db:
            uploaded, released = db.execute(
                "SELECT upload_received,released_at FROM room_edits WHERE transaction_id=?",
                (identifier,),
            ).fetchone()
            self.assertIsNone(uploaded)
            self.assertIsNotNone(released)
            self.assertFalse(awaiting_upload(db, self.robot, self.robot + "-100"))
        with patch("roomba_cloud.maps.room_edits.time.time", return_value=1):
            receive_result(
                self.shadows.path,
                self.robot,
                json.dumps({"transaction_id": identifier, "status": "error"}).encode(),
            )
        with contextlib.closing(sqlite3.connect(self.shadows.path)) as db:
            self.assertEqual(
                db.execute("SELECT released_at FROM room_edits").fetchone()[0], released
            )
        self.assertEqual(
            edit_status(self.shadows.path, self.robot, identifier)["status"], "deleted"
        )
        with contextlib.closing(sqlite3.connect(self.shadows.path)) as db:
            self.assertIsNone(db.execute("SELECT upload_received FROM room_edits").fetchone()[0])

    def test_bootstrap_payloads_retain_each_map_and_transaction(self):
        from unittest.mock import Mock

        maps = [self.robot + "-200", self.robot + "-300"]
        self.shadows.respond(
            f"$aws/things/{self.robot}/shadow/name/ro-currentstate/update",
            json.dumps(
                {
                    "state": {
                        "reported": {
                            "p2maps": [
                                {"p2map_id": identifier, "p2mapv_id": "1"} for identifier in maps
                            ]
                        }
                    }
                }
            ).encode(),
            self.robot,
        )
        session = Mock()
        documents = []
        session.publish_once.side_effect = lambda topic, document: documents.append(document)
        bootstrap_once(self.shadows.path, self.uploads.path, self.robot, session)
        payloads = [document() for document in documents]
        self.assertEqual([payload["p2map_id"] for payload in payloads], maps)
        self.assertEqual(len({payload["transaction_id"] for payload in payloads}), 2)

    def test_bootstrap_waits_for_fresh_inventory_and_runs_once_per_connection(self):
        from unittest.mock import Mock
        import roomba_cloud.mqtt_server as mqtt_probe

        session = Mock()
        session.map_bootstrap_attempted = False
        session.map_refresh_in_progress = False
        session.edit_topic = f"v011-irbthbu/things/{self.robot}/edit_req"
        session.subscriptions = {session.edit_topic}
        shadows = Mock()
        shadows.path = self.shadows.path
        shadows.respond.return_value = ("accepted", {"state": {}})
        topic = f"$aws/things/{self.robot}/shadow/name/ro-currentstate/update"
        documents = [
            {"state": {"reported": {"batPct": 50}}},
            {"state": {"reported": {"p2maps": []}}},
            {"state": {"reported": {"p2maps": []}}},
        ]
        payloads = [json.dumps(document).encode() for document in documents]
        frames = [(0x30, payload) for payload in payloads] + [(0xE0, b"")]
        events = [
            ("mqtt_publish", {"topic": topic, "payload_bytes": len(payload)}, None, False)
            for payload in payloads
        ] + [("mqtt_disconnect", {}, None, True)]
        with (
            patch.object(mqtt_probe, "read_frame", side_effect=frames * 2),
            patch.object(mqtt_probe, "observation_packet", side_effect=events * 2),
            patch.object(mqtt_probe.threading, "Thread") as thread,
            patch.object(mqtt_probe, "log"),
        ):
            mqtt_probe.observe(
                session,
                "test",
                {"client_id": self.robot, "keepalive_seconds": 30, "protocol_version": 5},
                bytearray(),
                None,
                shadows=shadows,
                map_uploads=self.uploads,
                command_session=session,
            )
            thread.assert_called_once()
            self.assertTrue(thread.call_args.kwargs["args"][-1])
            session.map_refresh_in_progress = False
            mqtt_probe.observe(
                session,
                "test",
                {"client_id": self.robot, "keepalive_seconds": 30, "protocol_version": 5},
                bytearray(),
                None,
                shadows=shadows,
                map_uploads=self.uploads,
                command_session=session,
            )
        self.assertTrue(session.map_bootstrap_attempted)
        self.assertEqual(thread.call_count, 2)
        self.assertFalse(thread.call_args.kwargs["args"][-1])
        self.assertEqual(thread.return_value.start.call_count, 2)

    def test_bootstrap_skips_existing_maps_and_sends_single_invalid_edit(self):
        from unittest.mock import Mock
        from roomba_cloud.robot_commands import CommandError

        session = Mock()
        with patch("roomba_cloud.maps.room_edits.log", create=True):
            bootstrap_once(self.shadows.path, self.uploads.path, self.robot, session)
        session.publish_once.assert_not_called()
        with contextlib.closing(sqlite3.connect(self.uploads.path)) as db, db:
            db.execute("DELETE FROM uploads")
        self.shadows.respond(
            f"$aws/things/{self.robot}/shadow/name/ro-currentstate/update",
            json.dumps(
                {
                    "state": {
                        "reported": {
                            "p2maps": [{"p2map_id": self.robot + "-2147483647", "p2mapv_id": "1"}]
                        }
                    }
                }
            ).encode(),
            self.robot,
        )
        payloads = []

        def send(topic, document):
            payloads.append(document())
            raise CommandError("timeout")

        session.publish_once.side_effect = send
        bootstrap_once(self.shadows.path, self.uploads.path, self.robot, session)
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["p2map_id"], self.robot + "-2147483647")
        self.assertEqual(payloads[0]["edit_cmd"]["command"], "rename_room")
        self.assertEqual(payloads[0]["edit_cmd"]["params"]["room_id"], "2147483647")

    def test_bootstrap_partial_inventory_deduplicates_and_does_not_retry_failed_map(self):
        from unittest.mock import Mock
        from roomba_cloud.robot_commands import CommandError

        known, first, second = [self.robot + "-" + str(n) for n in (100, 200, 300)]
        self.shadows.respond(
            f"$aws/things/{self.robot}/shadow/name/ro-currentstate/update",
            json.dumps(
                {
                    "state": {
                        "reported": {
                            "p2maps": [
                                {"p2map_id": key, "p2mapv_id": "1"}
                                for key in (known, first, first, second)
                            ]
                        }
                    }
                }
            ).encode(),
            self.robot,
        )
        payloads = []

        def send(topic, document):
            payloads.append(document())
            if len(payloads) == 1:
                raise CommandError("timeout")

        session = Mock()
        session.publish_once.side_effect = send
        with patch("roomba_cloud.maps.catalog.saved_maps", return_value={known: {}}):
            bootstrap_once(self.shadows.path, self.uploads.path, self.robot, session)
        self.assertEqual([item["p2map_id"] for item in payloads], [first, second])
        self.assertNotEqual(payloads[0]["transaction_id"], payloads[1]["transaction_id"])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.shadows, self.uploads, self.robot = populate(self.directory.name)
        initialize(self.shadows.path)
        self.shadows.respond(
            f"$aws/things/{self.robot}/shadow/name/ro-currentstate/update",
            b'{"state":{"reported":{"cleanMissionStatus":{"cycle":"none","phase":"charge"}}}}',
            self.robot,
        )

    def lock_map(self, map_id, *, robot_id=None, created=100):
        with contextlib.closing(sqlite3.connect(self.shadows.path)) as db, db:
            return db.execute(
                "INSERT INTO room_edits(robot_id,created,status,revision,map_id,archive_token) "
                "VALUES (?,?,'confirmed','revision',?,'old.tar.gz')",
                (robot_id or self.robot, created, map_id),
            ).lastrowid

    def set_mission(self, mission):
        topic = f"$aws/things/{self.robot}/shadow/name/ro-currentstate"
        with contextlib.closing(sqlite3.connect(self.shadows.path)) as db, db:
            row = db.execute("SELECT document FROM shadows WHERE topic=?", (topic,)).fetchone()
            document = json.loads(row[0])
            document["state"]["reported"]["cleanMissionStatus"] = mission
            db.execute("UPDATE shadows SET document=? WHERE topic=?", (json.dumps(document), topic))

    def test_inventory_probes_each_locked_map_only_once_per_connection(self):
        from unittest.mock import Mock

        target, missing = self.robot + "-100", self.robot + "-200"
        self.shadows.respond(
            f"$aws/things/{self.robot}/shadow/name/ro-currentstate/update",
            json.dumps(
                {
                    "state": {
                        "reported": {
                            "p2maps": [
                                {"p2map_id": key, "p2mapv_id": "1"} for key in (target, missing)
                            ]
                        }
                    }
                }
            ).encode(),
            self.robot,
        )
        self.lock_map(target)
        session = Mock()
        with patch("roomba_cloud.maps.catalog.saved_maps", return_value={target: {}}):
            for _ in range(2):
                bootstrap_once(
                    self.shadows.path, self.uploads.path, self.robot, session, include_missing=False
                )
        self.assertEqual(
            [call.args[1]()["p2map_id"] for call in session.publish_once.call_args_list],
            [target],
        )
        self.assertFalse(session.map_refresh_in_progress)
        self.lock_map(target, created=101)
        with patch("roomba_cloud.maps.catalog.saved_maps", return_value={target: {}}):
            bootstrap_once(
                self.shadows.path, self.uploads.path, self.robot, session, include_missing=False
            )
        self.assertEqual(session.publish_once.call_count, 2)
        with contextlib.closing(sqlite3.connect(self.shadows.path)) as db, db:
            db.execute("UPDATE room_edits SET released_at=200 WHERE map_id=?", (target,))
        session.reset_mock()
        with patch("roomba_cloud.maps.catalog.saved_maps", return_value={target: {}}):
            bootstrap_once(
                self.shadows.path, self.uploads.path, self.robot, session, include_missing=False
            )
        session.publish_once.assert_not_called()

    def test_any_transfer_with_geometry_clears_only_matching_pre_upload_locks_persistently(self):
        self.mission_map()
        for kind in self.uploads.TYPES:
            with self.subTest(kind=kind):
                identifier = self.lock_map("map-one")
                other = self.lock_map("map-two")
                foreign = self.lock_map("map-one", robot_id="c" * 32)
                later = self.lock_map("map-one", created=300)
                with self.uploads.connect() as db, db:
                    db.execute("UPDATE uploads SET xfer_type=? WHERE token=?", (kind, "d" * 64))
                clear_uploaded_locks(self.shadows.path, self.uploads.path, "d" * 64, 200)
                initialize(self.shadows.path)
                with contextlib.closing(sqlite3.connect(self.shadows.path)) as db, db:
                    cleared = dict(
                        db.execute("SELECT transaction_id,upload_received FROM room_edits")
                    )
                    self.assertEqual(cleared[identifier], 200)
                    self.assertEqual(
                        db.execute(
                            "SELECT released_at FROM room_edits WHERE transaction_id=?",
                            (identifier,),
                        ).fetchone()[0],
                        200,
                    )
                    for untouched in (other, foreign, later):
                        self.assertIsNone(cleared[untouched])
                    db.execute("DELETE FROM room_edits WHERE transaction_id=?", (later,))
                    self.assertFalse(awaiting_upload(db, self.robot, "map-one"))
                    db.execute("DELETE FROM room_edits")

    def test_report_only_unsupported_invalid_and_incomplete_uploads_do_not_unlock(self):
        from test_room_catalog import RoomCatalogTests

        fixture = RoomCatalogTests()
        fixture.directory, fixture.uploads, fixture.robot_id = (
            self.directory,
            self.uploads,
            self.robot,
        )
        self.lock_map("map-one")
        for options in ({"include_map": False}, {"map_type": 0}, {"map_id": "map-two"}):
            fixture.mission_archive(**options)
            clear_uploaded_locks(self.shadows.path, self.uploads.path, "d" * 64, 200)
            with contextlib.closing(sqlite3.connect(self.shadows.path)) as db:
                self.assertTrue(awaiting_upload(db, self.robot, "map-one"))
        # The baseline smart archive has room tables but no geometry.
        clear_uploaded_locks(self.shadows.path, self.uploads.path, "b" * 64, 200)
        with contextlib.closing(sqlite3.connect(self.shadows.path)) as db:
            self.assertTrue(awaiting_upload(db, self.robot, "map-one"))
        with self.uploads.connect() as db, db:
            db.execute("UPDATE uploads SET status='receiving' WHERE token=?", ("d" * 64,))
        with self.assertRaisesRegex(ValueError, "Completed"):
            clear_uploaded_locks(self.shadows.path, self.uploads.path, "d" * 64, 200)

    def test_upload_handler_persistently_unlocks_before_pruning_and_reply(self):
        import http.client
        from http.server import ThreadingHTTPServer
        from pathlib import Path
        import threading
        import time
        from roomba_cloud.maps.upload import MapUploadHandler

        self.mission_map()
        self.lock_map("map-one")
        token = "e" * 64
        payload = (Path(self.directory.name) / ("d" * 64 + ".tar.gz")).read_bytes()
        with self.uploads.connect() as db, db:
            db.execute(
                "INSERT INTO uploads VALUES (?,?,?,?,?,?,?,?)",
                (
                    token,
                    self.robot,
                    "uploadP2MapLive",
                    3,
                    int(time.time()) + 60,
                    "pending",
                    0,
                    None,
                ),
            )

        def prune(upload_token):
            self.assertEqual(upload_token, token)
            with contextlib.closing(sqlite3.connect(self.shadows.path)) as db:
                self.assertFalse(awaiting_upload(db, self.robot, "map-one"))

        with (
            ThreadingHTTPServer(("127.0.0.1", 0), MapUploadHandler) as server,
            patch.object(self.uploads, "prune_smart_maps", side_effect=prune) as pruned,
        ):
            server.uploads = self.uploads
            server.customer_shadow_db = self.shadows.path
            server.max_bytes = 65536
            server.max_total = 1024 * 1024
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
            thread.start()
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            try:
                conn.request("PUT", "/map-upload/" + token, payload)
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                response.read()
                pruned.assert_called_once_with(token)
                # Even removing all archive metadata cannot resurrect this lock.
                with self.uploads.connect() as db, db:
                    db.execute("DELETE FROM uploads")
                initialize(self.shadows.path)
                with contextlib.closing(sqlite3.connect(self.shadows.path)) as db:
                    self.assertFalse(awaiting_upload(db, self.robot, "map-one"))
            finally:
                conn.close()
                server.shutdown()
                thread.join(5)
            self.assertFalse(thread.is_alive())

    def test_legacy_schema_migration_preserves_transactions_and_locks(self):
        from pathlib import Path

        legacy = Path(self.directory.name) / "legacy.db"
        with contextlib.closing(sqlite3.connect(legacy)) as db, db:
            db.execute(
                "CREATE TABLE room_edits (transaction_id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "robot_id TEXT,created REAL,status TEXT,revision TEXT,map_id TEXT,archive_token TEXT)"
            )
            db.execute(
                "INSERT INTO room_edits VALUES (1,?,100,'rejected','digest','map','old.tar.gz')",
                (self.robot,),
            )
        initialize(legacy)
        initialize(legacy)
        with contextlib.closing(sqlite3.connect(legacy)) as db:
            self.assertTrue(awaiting_upload(db, self.robot, "map"))
            self.assertEqual(db.execute("SELECT status FROM room_edits").fetchone()[0], "rejected")

    def mission_map(self, adjacency=b"\0\1\1\0", map_id="map-one"):
        from test_room_catalog import RoomCatalogTests

        fixture = RoomCatalogTests()
        fixture.directory = self.directory
        fixture.uploads = self.uploads
        fixture.robot_id = self.robot
        fixture.mission_archive(adjacency=adjacency, map_id=map_id)
        # The other synthetic saved map contains rooms but no geometry.
        with contextlib.closing(sqlite3.connect(self.shadows.path)) as db, db:
            db.execute(
                "UPDATE shadows SET document=? WHERE topic=?",
                (
                    json.dumps(
                        {
                            "state": {
                                "reported": {
                                    "cleanMissionStatus": {"phase": "charge", "cycle": "none"},
                                    "p2maps": [{"p2map_id": map_id, "p2mapv_id": "1.0"}],
                                }
                            }
                        }
                    ),
                    f"$aws/things/{self.robot}/shadow/name/ro-currentstate",
                ),
            )
        return editor_maps(self.shadows.path, self.uploads.path, self.robot)["maps"][0]

    def test_editor_snapshot_uses_archive_version_and_native_coordinates(self):
        self.mission_map()
        result = editor_maps(self.shadows.path, self.uploads.path, self.robot)
        model = result["maps"][0]
        self.assertEqual((model["width"], model["height"]), (12, 10))
        self.assertEqual({room["name"] for room in model["rooms"]}, {"West", "East"})
        self.assertEqual(model["version"], "1.0")
        self.assertIn("split", model["operations"])
        absent = editor_maps(self.shadows.path, self.uploads.path, "c" * 32)
        self.assertEqual(absent["maps"], [])
        self.assertFalse(absent["map_controls"]["allowed"])

    def test_validation_rejects_stale_unknown_invalid_and_injected_edits(self):
        model = self.mission_map()
        edit = {
            "map_id": model["id"],
            "version": model["version"],
            "revision": model["revision"],
            "operation": "rename",
            "room_id": "1",
            "name": "Kitchen",
        }
        self.assertEqual(
            validate_edit(self.shadows.path, self.uploads.path, self.robot, edit).width, 12
        )
        for changes in (
            {"revision": "0" * 64},
            {"version": "old"},
            {"room_id": "999"},
            {"map_id": "unknown"},
            {"name": ""},
            {"name": "bad\nname"},
            {"operation": "delete"},
            {"params": {}},
            {"name": 1},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_edit(self.shadows.path, self.uploads.path, self.robot, edit | changes)
        split = {key: value for key, value in edit.items() if key != "name"}
        split.update(operation="split", points=[[2, 0], [2, 9]])
        validate_edit(self.shadows.path, self.uploads.path, self.robot, split)
        for points in (
            [[1, 1]],
            [[1, 1], [1, 1]],
            [[True, 1], [4, 5]],
            [[float("nan"), 1], [4, 5]],
            [[-1, 1], [4, 5]],
            [[1, 1], [4, 5]],
            [[7, 0], [7, 9]],
            [[0, 0], [0, 9]],
            [[2, 0], [2, 10]],
            [[float("inf"), 0], [2, 9]],
        ):
            with self.subTest(points=points), self.assertRaises(ValueError):
                validate_edit(
                    self.shadows.path, self.uploads.path, self.robot, split | {"points": points}
                )
        merge = {key: value for key, value in edit.items() if key != "name"}
        merge.update(operation="merge", other_room_id="2")
        validate_edit(self.shadows.path, self.uploads.path, self.robot, merge)
        with self.assertRaises(ValueError):
            validate_edit(
                self.shadows.path, self.uploads.path, self.robot, merge | {"other_room_id": "1"}
            )

    def test_idle_and_unknown_transaction_checks(self):
        self.shadows.respond(
            f"$aws/things/{self.robot}/shadow/name/ro-currentstate/update",
            b'{"state":{"reported":{"cleanMissionStatus":{"phase":"pause","cycle":"clean"}}}}',
            self.robot,
        )
        with self.assertRaisesRegex(ValueError, "idle"):
            require_idle(self.shadows.path, self.robot)
        with self.assertRaisesRegex(ValueError, "Unknown"):
            edit_status(self.shadows.path, self.robot, 1)

    def test_all_user_edits_and_automatic_probes_block_missions_and_unknown_state(self):
        from unittest.mock import Mock
        from roomba_cloud.maps.controls import map_control_state
        from roomba_cloud.robot_commands import CommandRegistry, CommandError

        snapshot = self.mission_map()
        base = {
            "map_id": snapshot["id"],
            "version": snapshot["version"],
            "revision": snapshot["revision"],
            "room_id": "1",
        }
        edits = [
            base | {"operation": "rename", "name": "Kitchen"},
            base | {"operation": "merge", "other_room_id": "2"},
            base | {"operation": "split", "points": [[2, 0], [2, 9]]},
            {key: value for key, value in base.items() if key != "room_id"}
            | {"operation": "delete"},
        ]
        for mission in (
            {},
            {"phase": "charge"},
            {"cycle": "none"},
            None,
            [],
            1,
            "",
            {"phase": "run", "cycle": "clean"},
            {"phase": "pause", "cycle": "clean"},
            {"phase": "stop", "cycle": "clean"},
            {"phase": "charge", "cycle": "clean"},
            {"phase": "hmMidMsn", "cycle": "clean"},
            {"phase": "future", "cycle": "none"},
        ):
            with self.subTest(mission=mission):
                self.set_mission(mission)
                self.assertFalse(map_control_state(self.shadows.path, self.robot)["allowed"])
                self.assertFalse(
                    editor_maps(self.shadows.path, self.uploads.path, self.robot)["map_controls"][
                        "allowed"
                    ]
                )
                session = Mock()
                registry = CommandRegistry(self.shadows.path, self.uploads.path)
                registry.register(self.robot, session)
                for edit in edits:
                    with self.assertRaisesRegex(ValueError, "no active or paused mission"):
                        submit(self.shadows.path, self.uploads.path, self.robot, session, edit)
                    with self.assertRaisesRegex(CommandError, "no active or paused mission"):
                        registry.execute(
                            {"robot_id": self.robot, "command": "room_edit", "edit": edit}
                        )
                bootstrap_once(self.shadows.path, self.uploads.path, self.robot, session)
                session.publish_once.assert_not_called()
        with contextlib.closing(sqlite3.connect(self.shadows.path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM room_edits").fetchone()[0], 0)
        for phase in ("charge", "stop", "idle"):
            self.set_mission({"phase": phase, "cycle": "none"})
            require_idle(self.shadows.path, self.robot)

    def test_mission_starting_before_payload_construction_never_reaches_wire_or_leaves_lock(self):
        from unittest.mock import Mock

        snapshot = self.mission_map()
        edit = {
            "map_id": self.robot + "-100",
            "version": snapshot["version"],
            "revision": snapshot["revision"],
            "operation": "rename",
            "room_id": "1",
            "name": "Kitchen",
        }

        @contextlib.contextmanager
        def fixture_catalog(*args):
            yield {edit["map_id"]: {"archive": ("/tmp/test.tar.gz", 1, 1, edit["revision"])}}

        wire = Mock()

        def publish(topic, document):
            self.set_mission({"cycle": "clean", "phase": "run"})
            wire(document())

        session = Mock()
        session.publish_once.side_effect = publish
        with (
            patch("roomba_cloud.maps.room_edits.validate_edit", return_value=None),
            patch("roomba_cloud.maps.room_edits.map_catalog", fixture_catalog),
        ):
            with self.assertRaisesRegex(ValueError, "no active or paused mission"):
                submit(self.shadows.path, self.uploads.path, self.robot, session, edit)
        wire.assert_not_called()
        with contextlib.closing(sqlite3.connect(self.shadows.path)) as db:
            self.assertFalse(awaiting_upload(db, self.robot, edit["map_id"]))
            uploaded, released = db.execute(
                "SELECT upload_received,released_at FROM room_edits"
            ).fetchone()
            self.assertIsNone(uploaded)
            self.assertIsNotNone(released)

    def test_map_lock_persists_across_initialization_and_blocks_editor_validation(self):
        model = self.mission_map()
        with contextlib.closing(sqlite3.connect(self.shadows.path)) as db, db:
            db.execute(
                "INSERT INTO room_edits(robot_id,created,status,revision,map_id,archive_token) "
                "VALUES (?,0,'rejected',?,?,?)",
                (self.robot, model["revision"], model["id"], "d" * 64 + ".tar.gz"),
            )
        initialize(self.shadows.path)
        snapshot = editor_maps(self.shadows.path, self.uploads.path, self.robot)["maps"][0]
        self.assertTrue(snapshot["awaiting_upload"])
        edit = {
            "map_id": model["id"],
            "version": model["version"],
            "revision": model["revision"],
            "operation": "rename",
            "room_id": "1",
            "name": "Kitchen",
        }
        with self.assertRaisesRegex(ValueError, "Waiting"):
            validate_edit(self.shadows.path, self.uploads.path, self.robot, edit)

    def test_merge_requires_explicit_adjacency_before_publishing(self):
        from unittest.mock import Mock

        for adjacency, reason in ((None, "unavailable"), (b"\0" * 4, "not adjacent")):
            with self.subTest(adjacency=adjacency):
                model = self.mission_map(adjacency)
                session = Mock()
                edit = {
                    "map_id": model["id"],
                    "version": model["version"],
                    "revision": model["revision"],
                    "operation": "merge",
                    "room_id": "1",
                    "other_room_id": "2",
                }
                with self.assertRaisesRegex(ValueError, reason):
                    submit(self.shadows.path, self.uploads.path, self.robot, session, edit)
                session.publish_once.assert_not_called()
                self.assertEqual(model["rooms"][0]["adjacent_room_ids"], [])
                self.assertIn("split", model["operations"])
        model = self.mission_map()
        self.assertEqual(model["rooms"][0]["adjacent_room_ids"], ["2"])

    def test_exact_verified_wire_payloads(self):
        base = {"map_id": "a" * 32 + "-1765480576", "room_id": "10"}
        self.assertEqual(
            wire_command(base | {"operation": "rename", "name": "Kitchen"}, None),
            {"command": "rename_room", "params": {"room_id": "10", "room_name": "Kitchen"}},
        )
        self.assertEqual(
            wire_command(base | {"operation": "merge", "other_room_id": "11"}, None),
            {"command": "arrange_room", "params": {"room_ids": ["10", "11"]}},
        )
        from dataclasses import replace
        from roomba_cloud.maps.preview import decode_geometry
        from test_map_preview import geometry

        model = replace(decode_geometry(geometry()), origin=(-3.25, 1.75), resolution=0.125)
        split = base | {"operation": "split", "points": [[2, 0], [2, 9]]}
        self.assertEqual(
            wire_command(split, model),
            {
                "command": "split_room",
                "params": {"room_id": "10", "split_points": [-3.0, 1.75, -3.0, 2.875]},
            },
        )
        reversed_split = split | {"points": list(reversed(split["points"]))}
        self.assertEqual(
            wire_command(reversed_split, model)["params"]["split_points"], [-3.0, 2.875, -3.0, 1.75]
        )
        for changes in (
            {"room_id": "256"},
            {"map_id": "fake"},
            {"operation": "unsupported"},
            {"other_room_id": "256"},
        ):
            edit = base | {"operation": "merge", "other_room_id": "11"} | changes
            with self.subTest(edit=edit), self.assertRaises(ValueError):
                wire_command(edit, None)

    def test_delete_exact_target_waits_for_inventory_not_ack_or_upload(self):
        from unittest.mock import Mock

        snapshot = self.mission_map(map_id=self.robot + "-100")
        edit = {key: snapshot[key] for key in ("version", "revision")}
        edit.update(map_id=snapshot["id"], operation="delete")
        session = Mock()
        payloads = []
        session.publish_once.side_effect = lambda topic, document: payloads.append(document())
        result = submit(self.shadows.path, self.uploads.path, self.robot, session, edit)
        identifier = result["transaction_id"]
        self.assertEqual(
            payloads,
            [
                {
                    "p2map_id": snapshot["id"],
                    "transaction_id": identifier,
                    "edit_cmd": {"command": "del_map", "params": {}},
                }
            ],
        )
        receive_result(
            self.shadows.path,
            self.robot,
            json.dumps({"transaction_id": identifier, "status": "success"}).encode(),
        )
        self.assertEqual(
            edit_status(self.shadows.path, self.robot, identifier)["status"], "pending"
        )
        clear_uploaded_locks(self.shadows.path, self.uploads.path, "d" * 64, 1e12)
        with contextlib.closing(sqlite3.connect(self.shadows.path)) as db:
            self.assertTrue(awaiting_upload(db, self.robot, snapshot["id"]))
        with self.assertRaisesRegex(ValueError, "Waiting for a new map upload"):
            submit(self.shadows.path, self.uploads.path, self.robot, session, edit)
        session.reset_mock()
        bootstrap_once(self.shadows.path, self.uploads.path, self.robot, session)
        session.publish_once.assert_not_called()
        with contextlib.closing(sqlite3.connect(self.shadows.path)) as db, db:
            db.execute(
                "UPDATE shadows SET document=? WHERE topic=?",
                (
                    '{"state":{"reported":{"p2maps":[]}}}',
                    f"$aws/things/{self.robot}/shadow/name/ro-currentstate",
                ),
            )
        self.assertEqual(
            edit_status(self.shadows.path, self.robot, identifier)["status"], "deleted"
        )
        self.assertEqual(editor_maps(self.shadows.path, self.uploads.path, self.robot)["maps"], [])
        from roomba_cloud.maps.catalog import saved_maps

        self.assertEqual(saved_maps(self.shadows.path, self.uploads.path, self.robot), {})
        with self.uploads.connect() as db:
            self.assertGreater(db.execute("SELECT count(*) FROM uploads").fetchone()[0], 0)

    def test_delete_rejection_releases_lock_and_validates_mission_and_identity(self):
        from unittest.mock import Mock

        snapshot = self.mission_map(map_id=self.robot + "-100")
        edit = {
            "map_id": snapshot["id"],
            "version": snapshot["version"],
            "revision": snapshot["revision"],
            "operation": "delete",
        }
        for changes in ({"map_id": "foreign"}, {"version": "stale"}, {"room_id": "1"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_edit(self.shadows.path, self.uploads.path, self.robot, edit | changes)
        self.set_mission({"phase": "pause", "cycle": "clean"})
        session = Mock()
        with self.assertRaisesRegex(ValueError, "no active or paused mission"):
            submit(self.shadows.path, self.uploads.path, self.robot, session, edit)
        session.publish_once.assert_not_called()
        self.set_mission({"phase": "charge", "cycle": "none"})
        session.publish_once.side_effect = lambda topic, document: document()
        identifier = submit(self.shadows.path, self.uploads.path, self.robot, session, edit)[
            "transaction_id"
        ]
        receive_result(
            self.shadows.path,
            self.robot,
            json.dumps({"transaction_id": identifier, "status": "error"}).encode(),
        )
        self.assertEqual(
            edit_status(self.shadows.path, self.robot, identifier)["status"], "rejected"
        )
        with contextlib.closing(sqlite3.connect(self.shadows.path)) as db:
            self.assertFalse(awaiting_upload(db, self.robot, edit["map_id"]))

    def test_split_guard_allows_boundary_crossings_but_rejects_tangency_and_multiple_cuts(self):
        box = [(0, 0), (5, 0), (5, 9), (0, 9)]
        for points in ([[2, 0], [2, 9]], [[0, 4], [7, 4]], [[0, 0], [5, 9]], [[7, 4], [0, 4]]):
            require_room_crossing(points, box)
        for points in ([[0, 0], [5, 0]], [[-1, 1], [1, -1]], [[1, 1], [4, 8]], [[7, 0], [7, 9]]):
            with self.subTest(points=points), self.assertRaisesRegex(ValueError, "single cut"):
                require_room_crossing(points, box)
        u_shape = [(0, 0), (8, 0), (8, 8), (6, 8), (6, 2), (2, 2), (2, 8), (0, 8)]
        with self.assertRaisesRegex(ValueError, "single cut"):
            require_room_crossing([[-1, 5], [9, 5]], u_shape)

    def test_split_submission_targets_exact_map_converts_units_and_locks_until_upload(self):
        from unittest.mock import Mock

        model = self.mission_map()
        target = self.robot + "-1765382228"
        with self.uploads.connect() as db:
            row = db.execute(
                "SELECT bytes,sha256 FROM uploads WHERE token=?", ("d" * 64,)
            ).fetchone()
        from pathlib import Path

        archive = (Path(self.directory.name) / ("d" * 64 + ".tar.gz"), row[0], 0, row[1])

        @contextlib.contextmanager
        def fixture_catalog(*args):
            yield {
                target: {
                    "version": model["version"],
                    "archive": archive,
                    "archive_version": "mission-version",
                }
            }

        from roomba_cloud.maps.customer_maps import geometry

        parsed = geometry(*archive, "map-one", "mission-version")
        session = Mock()
        edit = {
            "map_id": target,
            "version": model["version"],
            "revision": model["revision"],
            "operation": "split",
            "room_id": "1",
            "points": [[2, 0], [2, 9]],
        }
        with (
            patch("roomba_cloud.maps.room_edits.map_catalog", fixture_catalog),
            patch("roomba_cloud.maps.room_edits.geometry", return_value=parsed),
        ):
            result = submit(self.shadows.path, self.uploads.path, self.robot, session, edit)
            payload = session.publish_once.call_args.args[1]()
            self.assertEqual(payload["p2map_id"], target)
            self.assertEqual(payload["transaction_id"], result["transaction_id"])
            self.assertEqual(payload["edit_cmd"]["command"], "split_room")
            self.assertEqual(payload["edit_cmd"]["params"]["room_id"], "1")
            for actual, expected in zip(
                payload["edit_cmd"]["params"]["split_points"], [-0.9, -2.0, -0.9, -1.55]
            ):
                self.assertAlmostEqual(actual, expected, places=6)
            with self.assertRaisesRegex(ValueError, "Waiting"):
                submit(self.shadows.path, self.uploads.path, self.robot, session, edit)
            session.publish_once.assert_called_once()

    def test_submission_result_scope_busy_timeout_and_delivery_failure(self):
        from unittest.mock import Mock
        from roomba_cloud.robot_commands import CommandError

        session = Mock()
        session.edit_topic = "edit/topic"
        edit = {
            "map_id": "a" * 32 + "-1765480576",
            "room_id": "10",
            "operation": "rename",
            "name": "Kitchen",
            "revision": "b" * 64,
        }

        def publish(topic, document):
            self.assertEqual(topic, session.edit_topic)
            value = document()
            self.assertEqual(value["edit_cmd"]["params"]["room_name"], "Kitchen")

        session.publish_once.side_effect = publish
        fixture_maps = {edit["map_id"]: {"archive": ("/tmp/old.tar.gz", 1, 1, edit["revision"])}}

        @contextlib.contextmanager
        def fixture_catalog(*args):
            yield fixture_maps

        with (
            patch("roomba_cloud.maps.room_edits.validate_edit", return_value=None),
            patch("roomba_cloud.maps.room_edits.map_catalog", fixture_catalog),
        ):
            result = submit(self.shadows.path, self.uploads.path, self.robot, session, edit)
            identifier = result["transaction_id"]
            self.assertEqual(
                edit_status(self.shadows.path, self.robot, identifier)["status"], "pending"
            )
            with self.assertRaisesRegex(ValueError, "Waiting"):
                submit(self.shadows.path, self.uploads.path, self.robot, session, edit)
            payload = json.dumps({"transaction_id": identifier, "status": "unknown"}).encode()
            receive_result(self.shadows.path, "c" * 32, payload)
            self.assertEqual(
                edit_status(self.shadows.path, self.robot, identifier)["status"], "pending"
            )
            receive_result(self.shadows.path, self.robot, payload)
            self.assertEqual(
                edit_status(self.shadows.path, self.robot, identifier)["status"], "responded"
            )
            with contextlib.closing(sqlite3.connect(self.shadows.path)) as db, db:
                db.execute(
                    "UPDATE room_edits SET status='pending' WHERE transaction_id=?", (identifier,)
                )
            receive_result(
                self.shadows.path,
                self.robot,
                json.dumps({"transaction_id": identifier, "status": "success"}).encode(),
            )
            self.assertEqual(
                edit_status(self.shadows.path, self.robot, identifier)["status"], "confirmed"
            )
            with self.assertRaisesRegex(ValueError, "Waiting"):
                submit(self.shadows.path, self.uploads.path, self.robot, session, edit)
            fixture_maps[edit["map_id"]]["archive"] = ("/tmp/new.tar.gz", 1, 1, edit["revision"])
            with self.assertRaisesRegex(ValueError, "Waiting"):
                submit(self.shadows.path, self.uploads.path, self.robot, session, edit)
            with contextlib.closing(sqlite3.connect(self.shadows.path)) as db, db:
                db.execute("UPDATE room_edits SET released_at=1")
            session.publish_once.side_effect = CommandError("timeout")
            with self.assertRaisesRegex(CommandError, "uncertain"):
                submit(self.shadows.path, self.uploads.path, self.robot, session, edit)
        with contextlib.closing(sqlite3.connect(self.shadows.path)) as db:
            latest = db.execute("SELECT max(transaction_id) FROM room_edits").fetchone()[0]
        self.assertEqual(
            edit_status(self.shadows.path, self.robot, latest)["status"], "unconfirmed"
        )
