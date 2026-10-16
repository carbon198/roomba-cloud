"""Safe per-robot, per-map archive retention and multi-map catalog regressions."""

import contextlib
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from roomba_cloud.maps.upload import MapUploadService
from roomba_cloud.maps.catalog import catalog, room_command
from roomba_cloud.shadow_service import ShadowService
from test_room_catalog import binary_rooms


class ArchivePruningTests(unittest.TestCase):
    robot = "a" * 32

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.uploads = MapUploadService(self.root / "uploads.db")
        self.shadows = ShadowService(self.root / "shadows.db")
        self.counter = 0

    def upload(self, maps, robot=None, kind="uploadP2MapSmart", valid=True, finish=True):
        self.counter += 1
        token = f"{self.counter:064x}"
        path = self.root / (token + ".tar.gz")
        if valid:
            entries = []
            files = []
            for index, (map_id, version) in enumerate(maps):
                name = f"map-{index}.bin"
                entries.append(
                    {
                        "type": "p2mapv",
                        "name": name,
                        "p2mapv": {"p2map_id": map_id, "p2mapv_id": version, "p2map_type": 3},
                    }
                )
                files.append((name, binary_rooms([(1, f"Room on {map_id}")])))
            files.append(
                ("manifest.json", json.dumps({"schema_version": 1, "files": entries}).encode())
            )
            with tarfile.open(path, "w") as archive:
                for name, raw in files:
                    member = tarfile.TarInfo(name)
                    member.size = len(raw)
                    archive.addfile(member, io.BytesIO(raw))
        else:
            path.write_bytes(b"invalid archive")
        with self.uploads.connect() as db, db:
            db.execute(
                "INSERT INTO uploads VALUES (?,?,?,?,?,?,?,?)",
                (
                    token,
                    robot or self.robot,
                    kind,
                    self.counter,
                    int(time.time()) + 900,
                    "receiving",
                    path.stat().st_size,
                    None,
                ),
            )
        if finish:
            self.complete(token)
        return token, path

    def complete(self, token):
        path = self.root / (token + ".tar.gz")
        self.uploads.finish(token, "complete", hashlib.sha256(path.read_bytes()).hexdigest())
        with contextlib.redirect_stdout(io.StringIO()):
            self.uploads.prune_smart_maps(token)

    def rows(self):
        with self.uploads.connect() as db:
            return {row[0]: row[1:] for row in db.execute("SELECT token,status,bytes FROM uploads")}

    def report_maps(self, maps):
        self.shadows.respond(
            f"$aws/things/{self.robot}/shadow/name/ro-currentstate/update",
            json.dumps(
                {
                    "state": {
                        "reported": {
                            "p2maps": [
                                {"p2map_id": map_id, "p2mapv_id": version}
                                for map_id, version in maps
                            ]
                        }
                    }
                }
            ).encode(),
            self.robot,
        )

    def test_replaces_same_map_preserves_other_maps_robots_and_mission_live(self):
        old, old_path = self.upload([("downstairs", "1")])
        other, other_path = self.upload([("upstairs", "1")])
        foreign, foreign_path = self.upload([("downstairs", "1")], robot="c" * 32)
        mission, mission_path = self.upload([("downstairs", "1")], kind="uploadP2MapMission")
        live, live_path = self.upload([("downstairs", "1")], kind="uploadP2MapLive")
        new, new_path = self.upload([("downstairs", "2")])
        self.assertFalse(old_path.exists())
        self.assertNotIn(old, self.rows())
        for token, path in (
            (other, other_path),
            (foreign, foreign_path),
            (mission, mission_path),
            (live, live_path),
            (new, new_path),
        ):
            self.assertTrue(path.exists())
            self.assertEqual(self.rows()[token][0], "complete")
        self.report_maps([("downstairs", "2"), ("upstairs", "1")])
        segments = catalog(self.shadows.path, self.uploads.path, self.robot)
        self.assertEqual({s["map_id"] for s in segments}, {"downstairs", "upstairs"})
        self.assertEqual(len({s["id"] for s in segments}), 2)
        self.assertEqual(len({s["group"] for s in segments}), 2)
        self.assertEqual(
            room_command(self.shadows.path, self.uploads.path, self.robot, ["upstairs:1:1"])[
                "p2map_id"
            ],
            "upstairs",
        )
        with self.assertRaises(ValueError):
            room_command(
                self.shadows.path, self.uploads.path, self.robot, ["downstairs:2:1", "upstairs:1:1"]
            )

    def test_multi_map_archive_retained_until_every_map_is_replaced(self):
        old, old_path = self.upload([("a", "1"), ("b", "1")])
        self.upload([("a", "2")])
        self.assertTrue(old_path.exists())
        self.report_maps([("a", "2"), ("b", "1")])
        self.assertEqual(
            {s["id"] for s in catalog(self.shadows.path, self.uploads.path, self.robot)},
            {"a:2:1", "b:1:1"},
        )
        self.upload([("b", "2")])
        self.assertFalse(old_path.exists())
        self.assertNotIn(old, self.rows())

    def test_invalid_or_unfinished_replacement_never_deletes_verified_map(self):
        old, old_path = self.upload([("a", "1")])
        self.upload([("a", "2")], valid=False)
        pending, _ = self.upload([("a", "3")], finish=False)
        with contextlib.redirect_stdout(io.StringIO()):
            self.uploads.prune_smart_maps(pending)
        self.assertTrue(old_path.exists())
        self.assertEqual(self.rows()[old][0], "complete")
        self.uploads.finish(pending, "failed")
        self.assertTrue(old_path.exists())

    def test_completion_order_not_negotiation_order_determines_newest(self):
        first, first_path = self.upload([("a", "1")], finish=False)
        second, second_path = self.upload([("a", "2")], finish=False)
        self.complete(second)
        self.complete(first)
        self.assertTrue(first_path.exists())
        self.assertFalse(second_path.exists())
        self.assertIn(first, self.rows())
        self.assertNotIn(second, self.rows())

    def test_delete_failure_retries_and_reclaims_storage_only_after_success(self):
        old, old_path = self.upload([("a", "1")])
        original_unlink = Path.unlink

        def fail_old(path, *args, **kwargs):
            if path == old_path:
                raise PermissionError("Test failure")
            return original_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", fail_old):
            new, new_path = self.upload([("a", "2")])
        self.assertEqual(self.rows()[old][0], "pruning")
        self.assertTrue(old_path.exists())
        with contextlib.redirect_stdout(io.StringIO()):
            self.uploads.heartbeat()
        self.assertFalse(old_path.exists())
        self.assertEqual(set(self.rows()), {new})
        self.assertEqual(sum(row[1] for row in self.rows().values()), new_path.stat().st_size)
        with self.uploads.connect() as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM smart_archive_order").fetchone()[0], 1
            )
        # Reopening the service/heartbeat also recovers a crash after unlink.
        with self.uploads.connect() as db, db:
            db.execute("UPDATE uploads SET status='pruning' WHERE token=?", (new,))
        new_path.unlink()
        with contextlib.redirect_stdout(io.StringIO()):
            MapUploadService(self.uploads.path).heartbeat()
        self.assertFalse(self.rows())

    def test_pruning_waits_for_inflight_catalog_transaction(self):
        _, old_path = self.upload([("a", "1")])
        reader = sqlite3.connect(self.uploads.path)
        self.addCleanup(reader.close)
        token, _ = self.upload([("a", "2")], finish=False)
        self.uploads.finish(
            token,
            "complete",
            hashlib.sha256((self.root / (token + ".tar.gz")).read_bytes()).hexdigest(),
        )
        reader.execute("BEGIN")
        reader.execute("SELECT token FROM uploads").fetchall()
        entered = threading.Event()
        done = threading.Event()
        errors = []

        def prune():
            entered.set()
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    self.uploads.prune_smart_maps(token)
            except Exception as exc:
                errors.append(exc)
            finally:
                done.set()

        thread = threading.Thread(target=prune)
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertFalse(done.wait(0.1))
            self.assertTrue(old_path.exists())
            reader.rollback()
            self.assertTrue(done.wait(3))
            self.assertFalse(old_path.exists())
            self.assertFalse(errors)
        finally:
            reader.rollback()
            thread.join(5)


if __name__ == "__main__":
    unittest.main()
