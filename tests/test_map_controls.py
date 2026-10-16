import contextlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from roomba_cloud.maps.controls import map_control_state, require_no_mission


class MapControlsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = Path(self.directory.name) / "shadows.db"
        with contextlib.closing(sqlite3.connect(self.database)) as db:
            db.execute("CREATE TABLE shadows (topic TEXT PRIMARY KEY, document TEXT NOT NULL)")

    def store(self, document):
        with contextlib.closing(sqlite3.connect(self.database)) as db, db:
            db.execute(
                "INSERT OR REPLACE INTO shadows VALUES (?, ?)",
                ("$aws/things/test/shadow/name/ro-currentstate", json.dumps(document)),
            )

    def test_only_explicit_idle_states_allow_controls(self):
        for phase in ("charge", "stop", "idle"):
            self.store(
                {"state": {"reported": {"cleanMissionStatus": {"cycle": "none", "phase": phase}}}}
            )
            self.assertEqual(
                map_control_state(self.database, "test"), {"allowed": True, "reason": ""}
            )
            require_no_mission(self.database, "test")

    def test_unknown_active_paused_and_malformed_missions_are_denied(self):
        self.assertFalse(map_control_state(self.database, "test")["allowed"])
        for mission in (
            {},
            None,
            [],
            {"cycle": "clean", "phase": "run"},
            {"cycle": "clean", "phase": "pause"},
            {"cycle": "clean", "phase": "charge"},
            {"cycle": "none", "phase": "unknown"},
        ):
            with self.subTest(mission=mission):
                self.store({"state": {"reported": {"cleanMissionStatus": mission}}})
                with self.assertRaisesRegex(ValueError, "known idle"):
                    require_no_mission(self.database, "test")

    def test_corrupt_persisted_shapes_are_explicit_errors(self):
        for document in ([], {"state": None}, {"state": {"reported": []}}):
            self.store(document)
            with self.assertRaisesRegex(ValueError, "Invalid persisted"):
                require_no_mission(self.database, "test")

    def test_database_failure_propagates_and_does_not_create_database(self):
        missing = Path(self.directory.name) / "missing.db"
        with self.assertRaises(sqlite3.OperationalError):
            require_no_mission(missing, "test")
        self.assertFalse(missing.exists())
