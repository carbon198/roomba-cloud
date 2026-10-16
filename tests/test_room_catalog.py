"""Synthetic, non-private map fixtures and exact targeted-command regressions."""

import contextlib
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import threading
import unittest
from unittest.mock import patch
import zlib

from roomba_cloud.maps.upload import MapUploadService
from roomba_cloud.maps.catalog import catalog, decode_rooms, read_archive, room_command, wire_fields
from roomba_cloud.robot_commands import (
    CommandError,
    CommandRegistry,
    CommandServer,
    CommandSession,
    request_command,
)
from roomba_cloud.shadow_service import ShadowService
from test_robot_commands import FakeRobot


def integer(value):
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def field(number, value):
    if isinstance(value, int):
        return integer(number << 3) + integer(value)
    return integer((number << 3) | 2) + integer(len(value)) + value


def binary_rooms(rooms):
    return zlib.compress(
        b"".join(
            field(12, field(1, identifier) + field(2, name.encode()) + field(10, 1))
            for identifier, name in rooms
        )
    )


def populate(directory):
    shadow_db = Path(directory) / "shadows.db"
    upload_db = Path(directory) / "uploads.db"
    service = ShadowService(shadow_db)
    uploads = MapUploadService(upload_db)
    robot_id = "a" * 32
    service.respond(
        f"$aws/things/{robot_id}/shadow/name/ro-currentstate/update",
        b'{"state":{"reported":{"batPct":80,"p2maps":['
        b'{"p2map_id":"map-one","p2mapv_id":"1.0"},'
        b'{"p2map_id":"map-two","p2mapv_id":"2.0"}]}}}',
        robot_id,
    )
    manifest = {
        "schema_version": 1,
        "files": [
            {
                "name": "one.bin",
                "type": "p2mapv",
                "p2mapv": {"p2map_id": "map-one", "p2mapv_id": "1.0", "p2map_type": 3},
            },
            {
                "name": "two.bin",
                "type": "p2mapv",
                "p2mapv": {"p2map_id": "map-two", "p2mapv_id": "2.0", "p2map_type": 3},
            },
        ],
    }
    path = Path(directory) / ("b" * 64 + ".tar.gz")
    with tarfile.open(path, "w") as archive:
        for name, raw in (
            ("manifest.json", json.dumps(manifest).encode()),
            ("one.bin", binary_rooms([(1, "Room one"), (2, "Room two")])),
            ("two.bin", binary_rooms([(1, "Upstairs")])),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            archive.addfile(info, io.BytesIO(raw))
    with uploads.connect() as db, db:
        db.execute(
            "INSERT INTO uploads VALUES (?,?,?,?,?,?,?,?)",
            (
                "b" * 64,
                robot_id,
                "uploadP2MapSmart",
                1,
                0,
                "complete",
                path.stat().st_size,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            ),
        )
    return service, uploads, robot_id


class RoomCatalogTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.service, self.uploads, self.robot_id = populate(self.directory.name)
        read_archive.cache_clear()

    def mission_archive(
        self,
        *,
        map_id="map-one",
        version="mission-version",
        map_type=21,
        robot_id=None,
        include_map=True,
        adjacency=None,
    ):
        from test_map_preview import geometry

        manifest = {
            "schema_version": 1,
            "files": [{"name": "mission_report.json", "type": "mission"}],
        }
        members = [("mission_report.json", b"{}")]
        if include_map:
            manifest["files"].append(
                {
                    "name": "p2map.bin",
                    "type": "p2mapv",
                    "p2mapv": {"p2map_id": map_id, "p2mapv_id": version, "p2map_type": map_type},
                }
            )
            members.append(("p2map.bin", geometry(adjacency=adjacency)))
        path = Path(self.directory.name) / ("d" * 64 + ".tar.gz")
        with tarfile.open(path, "w") as archive:
            for name, raw in [("manifest.json", json.dumps(manifest).encode()), *members]:
                info = tarfile.TarInfo(name)
                info.size = len(raw)
                archive.addfile(info, io.BytesIO(raw))
        with self.uploads.connect() as db, db:
            db.execute(
                "INSERT OR REPLACE INTO uploads VALUES (?,?,?,?,?,?,?,?)",
                (
                    "d" * 64,
                    robot_id or self.robot_id,
                    "uploadP2MapMission",
                    2,
                    0,
                    "complete",
                    path.stat().st_size,
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                ),
            )

    def test_mission_geometry_rooms_image_and_saved_version_command(self):
        from roomba_cloud.maps.customer_maps import descriptors, image
        from roomba_cloud.maps.catalog import saved_maps

        self.mission_archive()
        maps = saved_maps(self.service.path, self.uploads.path, self.robot_id)
        self.assertEqual(maps["map-one"]["rooms"], {"1": "West", "2": "East"})
        self.assertEqual(maps["map-one"]["archive_version"], "mission-version")
        self.assertEqual(maps["map-one"]["version"], "1.0")
        target = room_command(
            self.service.path, self.uploads.path, self.robot_id, ["map-one:1.0:2"]
        )
        self.assertEqual(target["user_p2mapv_id"], "1.0")
        descriptor = next(item for item in descriptors(maps) if item["id"] == "map-one")
        result = image(
            self.service.path,
            self.uploads.path,
            self.robot_id,
            "map-one",
            descriptor["version"],
            descriptor["revision"],
            "room",
        )
        self.assertTrue(result.startswith(b"\x89PNG\r\n\x1a\n"))
        with self.assertRaisesRegex(ValueError, "non-map|unverified"):
            item = maps["map-one"]
            read_archive(*item["archive"], smart_only=True)

    def test_mission_bootstrap_without_smart_archive_and_inventory_removal(self):
        from roomba_cloud.maps.catalog import saved_maps

        with self.uploads.connect() as db, db:
            db.execute("DELETE FROM uploads")
        self.mission_archive()
        self.assertEqual(len(catalog(self.service.path, self.uploads.path, self.robot_id)), 2)
        self.service.respond(
            f"$aws/things/{self.robot_id}/shadow/name/ro-currentstate/update",
            b'{"state":{"reported":{"p2maps":[]}}}',
            self.robot_id,
        )
        self.assertFalse(saved_maps(self.service.path, self.uploads.path, self.robot_id))

    def test_mission_report_only_unknown_type_identity_and_robot_are_not_promoted(self):
        baseline = catalog(self.service.path, self.uploads.path, self.robot_id)
        for options in (
            {"include_map": False},
            {"map_type": 0},
            {"map_id": "unknown-map"},
            {"robot_id": "c" * 32},
        ):
            with self.subTest(options=options):
                self.mission_archive(**options)
                self.assertEqual(
                    catalog(self.service.path, self.uploads.path, self.robot_id), baseline
                )

    def test_complete_catalog_map_scoped_ids_and_exact_command(self):
        segments = catalog(self.service.path, self.uploads.path, self.robot_id)
        self.assertEqual(len(segments), 3)
        self.assertEqual({item["name"] for item in segments}, {"Room one", "Room two", "Upstairs"})
        self.assertEqual(
            room_command(
                self.service.path,
                self.uploads.path,
                self.robot_id,
                ["map-one:1.0:2", "map-one:1.0:1"],
            ),
            {
                "p2map_id": "map-one",
                "user_p2mapv_id": "1.0",
                "select_all": False,
                "regions": [{"region_id": "2", "type": "rid"}, {"region_id": "1", "type": "rid"}],
            },
        )
        self.assertFalse(catalog(self.service.path, self.uploads.path, "c" * 32))
        self.assertFalse(catalog(self.service.path, None, self.robot_id))

    def test_empty_duplicate_unknown_and_cross_map_selection_never_sends(self):
        robot = FakeRobot()
        session = CommandSession(robot, 5, self.robot_id)
        robot.session = session
        session.subscriptions.add(session.topic)
        registry = CommandRegistry(self.service.path, self.uploads.path)
        registry.register(self.robot_id, session)
        for selection in (
            [],
            ["map-one:1.0:1"] * 2,
            ["unknown"],
            ["map-one:1.0:1", "map-two:2.0:1"],
            "map-one:1.0:1",
        ):
            with self.subTest(selection=selection), self.assertRaises(ValueError):
                registry.execute(
                    {
                        "robot_id": self.robot_id,
                        "command": "clean_segments",
                        "segment_ids": selection,
                    }
                )
        self.assertFalse(robot.payloads)
        self.service.respond(
            f"$aws/things/{self.robot_id}/shadow/name/ro-currentstate/update",
            b'{"state":{"reported":{"p2maps":[{"p2map_id":"map-one","p2mapv_id":"new"}]}}}',
            self.robot_id,
        )
        self.assertFalse(catalog(self.service.path, self.uploads.path, self.robot_id))
        with self.assertRaises(ValueError):
            registry.execute(
                {
                    "robot_id": self.robot_id,
                    "command": "clean_segments",
                    "segment_ids": ["map-one:1.0:1"],
                }
            )
        self.assertFalse(robot.payloads)

    def test_real_ipc_exact_qos_acknowledged_room_payload_no_raw_command_injection(self):
        robot = FakeRobot()
        session = CommandSession(robot, 5, self.robot_id)
        robot.session = session
        session.subscriptions.add(session.topic)
        registry = CommandRegistry(self.service.path, self.uploads.path)
        registry.register(self.robot_id, session)
        with CommandServer(Path(self.directory.name) / "commands.sock", registry) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                request_command(server.path, self.robot_id, "clean_segments", ["map-one:1.0:2"])
                payload = robot.payloads[0][1]
                self.assertEqual(
                    payload,
                    {
                        "command": "start",
                        "initiator": "localApp",
                        "time": payload["time"],
                        "p2map_id": "map-one",
                        "user_p2mapv_id": "1.0",
                        "select_all": False,
                        "regions": [{"region_id": "2", "type": "rid"}],
                    },
                )
                with self.assertRaises(CommandError), contextlib.redirect_stdout(io.StringIO()):
                    request_command(server.path, self.robot_id, "clean_segments", [])
                self.assertEqual(len(robot.payloads), 1)
                with self.assertRaises(CommandError):
                    registry.execute(
                        {
                            "robot_id": self.robot_id,
                            "command": "start",
                            "regions": [{"region_id": "2", "type": "rid"}],
                        }
                    )
            finally:
                server.shutdown()
                thread.join()

    def test_archive_integrity_no_extraction_and_cache(self):
        first = catalog(self.service.path, self.uploads.path, self.robot_id)
        before = set(Path(self.directory.name).iterdir())
        self.assertEqual(first, catalog(self.service.path, self.uploads.path, self.robot_id))
        self.assertGreater(read_archive.cache_info().hits, 0)
        self.assertEqual(before, set(Path(self.directory.name).iterdir()))
        with self.uploads.connect() as db, db:
            db.execute("UPDATE uploads SET sha256=?", ("0" * 64,))
        with self.assertRaisesRegex(ValueError, "digest"):
            catalog(self.service.path, self.uploads.path, self.robot_id)

    def test_bounded_decoder_rejects_malformed_maps(self):
        self.assertEqual(
            decode_rooms(binary_rooms([(1, ""), (2, "Kitchen")])),
            {"1": "Segment 1", "2": "Kitchen"},
        )
        for raw in (
            b"",
            b"\0",
            b"\x62\xff",
            b"\x62\x02\x08\x00",
            field(12, field(1, 1)) * 2,
            field(12, field(1, 1) + field(2, b"\xff")),
            field(12, field(1, 1) + field(2, b"name\n")),
            field(12, field(1, 1) + field(1, 2)),
            b"\x0b",
        ):
            with self.subTest(raw=raw), self.assertRaises((ValueError, UnicodeDecodeError)):
                decode_rooms(zlib.compress(raw))
        for compressed in (
            b"not zlib",
            binary_rooms([(1, "one")]) + b"trailing",
            binary_rooms([(1, "one")])[:-1],
        ):
            with self.assertRaises((ValueError, zlib.error)):
                decode_rooms(compressed)
        with patch("roomba_cloud.maps.catalog.MAX_MAP_BYTES", 10), self.assertRaises(ValueError):
            decode_rooms(binary_rooms([(1, "Room one")]))
        with self.assertRaises(ValueError):
            list(wire_fields(b"\x80" * 11))


if __name__ == "__main__":
    unittest.main()
