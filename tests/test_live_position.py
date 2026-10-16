import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import io

from PIL import Image

from roomba_cloud.maps.live_position import snapshot, overlays, descriptors
from roomba_cloud.shadow_service import ShadowService
from roomba_cloud.maps.preview import decode_geometry, render_png
from test_map_preview import geometry


class LivePositionTests(unittest.TestCase):
    def test_customer_device_metadata_is_reported_only_and_robot_scoped(self):
        from roomba_cloud.customer_telemetry import snapshot as telemetry

        self.service.respond(
            f"$aws/things/{self.robot}/shadow/update",
            json.dumps(
                {
                    "state": {
                        "reported": {
                            "sku": "N186020",
                            "serialNum": "TEST-SERIAL",
                            "password": "private",
                        }
                    }
                }
            ).encode(),
            self.robot,
        )
        self.service.respond(
            f"$aws/things/{self.robot}/shadow/name/rw-software/update",
            json.dumps({"state": {"reported": {"softwareVer": "test-firmware"}}}).encode(),
            self.robot,
        )
        result = telemetry(self.path, self.robot)
        self.assertEqual(
            result["device"],
            {
                "model": "N186020",
                "serial_number": "TEST-SERIAL",
                "sw_version": "test-firmware",
            },
        )
        self.assertIsNone(telemetry(self.path, "b" * 32))

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "shadow.db"
        self.service = ShadowService(self.path)
        self.robot = "a" * 32
        self.prefix = f"v011-irbthbu/things/{self.robot}/"
        self.clock = patch("time.time", return_value=1000)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.service.start_session(self.robot, 120)
        self.report_mission(1, "one")

    def report_mission(self, n, identifier, phase="run"):
        self.service.respond(
            f"$aws/things/{self.robot}/shadow/name/ro-currentstate/update",
            json.dumps(
                {
                    "state": {
                        "reported": {
                            "cleanMissionStatus": {
                                "nMssn": n,
                                "missionId": identifier,
                                "phase": phase,
                            }
                        }
                    }
                }
            ).encode(),
            self.robot,
        )

    def send(self, suffix, data):
        self.service.respond(self.prefix + suffix, json.dumps(data).encode(), self.robot)

    def relocate(self, n=1, identifier="one", map_id="map-one"):
        self.send(
            "mission/timeline/report",
            {
                "nMssn": n,
                "mission_id": identifier,
                "event": [
                    {
                        "type": "reloc",
                        "ts": 999,
                        "reloc": {"p2mapId": map_id, "p2mapvId": "different-live-version"},
                    }
                ],
            },
        )

    def position(self, sequence=1, timestamp=1000):
        self.send(
            "livemap/update",
            {
                "pos_update": {"cur_path": [sequence, -0.9, -1.85, 0, 5, timestamp]},
                "timestamp": timestamp,
                "update_expire_ts": 1060,
            },
        )

    def test_mission_map_id_freshness_expiry_and_no_cross_mission_reuse(self):
        self.position()
        self.assertIsNone(snapshot(self.path, self.robot))
        self.relocate()
        self.position(2)
        pose = snapshot(self.path, self.robot)
        self.assertEqual(pose["map_id"], "map-one")
        self.assertEqual(pose["sequence"], 2)
        self.position(1)
        self.assertEqual(snapshot(self.path, self.robot)["sequence"], 2)
        with patch("time.time", return_value=1011):
            self.assertIsNone(snapshot(self.path, self.robot))
        self.report_mission(2, "two")
        self.assertIsNone(snapshot(self.path, self.robot))
        self.relocate()  # A replayed old mission cannot select its map.
        self.position(3)
        self.assertIsNone(snapshot(self.path, self.robot))
        self.relocate(2, "two", "map-two")
        self.position(4)
        self.assertEqual(snapshot(self.path, self.robot)["map_id"], "map-two")
        self.report_mission(2, "two", "charge")
        self.assertIsNone(snapshot(self.path, self.robot))

    def test_localized_map_diagnostic_requires_fresh_position_and_robot_inventory(self):
        from roomba_cloud.customer_telemetry import snapshot as telemetry

        def state():
            return telemetry(self.path, self.robot)

        self.assertIsNone(state()["map_inventory"])
        self.service.respond(
            f"$aws/things/{self.robot}/shadow/name/ro-currentstate/update",
            b'{"state":{"reported":{"p2maps":[{"p2map_id":"map-one","p2mapv_id":"1"}]}}}',
            self.robot,
        )
        self.assertEqual(state()["map_inventory"], ["map-one"])
        self.assertFalse(state()["available"]["localized_map"])
        self.relocate()
        self.position()
        self.assertEqual(state()["values"]["localized_map"], "map-one")
        self.assertTrue(state()["available"]["localized_map"])
        self.assertEqual(
            next(item for item in state()["fields"] if item["key"] == "localized_map")[
                "entity_category"
            ],
            "diagnostic",
        )
        with patch("time.time", return_value=1011):
            self.assertFalse(state()["available"]["localized_map"])
        self.report_mission(2, "two")
        self.assertFalse(state()["available"]["localized_map"])
        self.relocate(2, "two")
        self.position()
        self.assertTrue(state()["available"]["localized_map"])
        self.report_mission(2, "two", "charge")
        self.assertFalse(state()["available"]["localized_map"])
        self.service.respond(
            f"$aws/things/{self.robot}/shadow/name/ro-currentstate/update",
            b'{"state":{"reported":{"p2maps":[]}}}',
            self.robot,
        )
        self.assertEqual(state()["map_inventory"], [])
        self.assertFalse(state()["available"]["localized_map"])

    def test_invalid_batches_are_rejected_without_overwriting_good_position(self):
        self.relocate()
        self.position()
        for path in (
            [],
            [1, 2, 3],
            [1, True, 2, 0, 0, 1000],
            [1, float("nan"), 2, 0, 0, 1000],
            [1, 2, 3, 0, 0, 100],
        ):
            with self.assertRaises(ValueError):
                self.send(
                    "livemap/update", {"pos_update": {"cur_path": path}, "update_expire_ts": 1060}
                )
        self.assertEqual(snapshot(self.path, self.robot)["sequence"], 1)
        self.send("livemap/update", {"map_update": {}})
        self.assertEqual(snapshot(self.path, self.robot)["sequence"], 1)

    def test_robot_marker_world_coordinates_and_heading(self):
        model = decode_geometry(geometry(dock=False))
        raw = render_png(model, size=256, position=(-0.9, -1.85, 0))
        with Image.open(io.BytesIO(raw)) as image:
            self.assertLess(
                max(abs(a - b) for a, b in zip(image.getpixel((74, 151)), (50, 180, 255))), 25
            )
            self.assertLessEqual(max(abs(c - 240) for c in image.getpixel((74, 146))), 2)
        for position in ((100, 100, 0), (float("nan"), 0, 0)):
            with self.assertRaises(ValueError):
                render_png(model, position=position)

    def test_batched_trail_survives_restart_completion_and_resets_next_mission(self):
        self.relocate()
        self.send(
            "livemap/update",
            {
                "pos_update": {"cur_path": [1, -0.9, -1.85, 0, 0, -0.8, -1.8, 0, 5, 1000]},
                "update_expire_ts": 1060,
            },
        )
        item = overlays(self.path, self.robot)["map-one"]
        self.assertEqual(item["points"], [[-0.9, -1.85, 0, True], [-0.8, -1.8, 5, False]])
        # Instantiate the service again: history lives in SQLite, not a process cache.
        ShadowService(self.path)
        self.assertEqual(overlays(self.path, self.robot)["map-one"]["points"], item["points"])
        self.report_mission(1, "one", "charge")
        completed = overlays(self.path, self.robot)["map-one"]
        self.assertEqual(completed["points"], item["points"])
        self.assertEqual(completed["status"], "docked")
        self.report_mission(1, "one", "idle")
        with patch("time.time", return_value=1100):
            self.assertEqual(overlays(self.path, self.robot)["map-one"]["points"], item["points"])
        self.report_mission(2, "two")
        self.assertFalse(overlays(self.path, self.robot))
        self.relocate(2, "two")
        self.position()
        self.assertEqual(len(overlays(self.path, self.robot)["map-one"]["points"]), 1)
        self.assertGreater(overlays(self.path, self.robot)["map-one"]["revision"], item["revision"])
        self.report_mission(3, "three")
        self.report_mission(3, "three", "charge")
        self.assertFalse(overlays(self.path, self.robot))

    def test_gaps_relocations_unknown_activity_and_bounded_truncation(self):
        self.relocate()
        self.position()
        self.position(3)  # Missing sequence: start a new line.
        points = overlays(self.path, self.robot)["map-one"]["points"]
        self.assertEqual([p[3] for p in points], [True, True])
        self.relocate(map_id="map-two")
        self.position(4)
        self.assertEqual(overlays(self.path, self.robot)["map-two"]["points"][0][3], True)
        self.assertEqual(len(overlays(self.path, self.robot)["map-one"]["points"]), 2)
        with patch("roomba_cloud.maps.live_position.MAX_TRAIL_POINTS", 3):
            self.position(5)
        result = descriptors(self.path, self.robot)
        self.assertEqual(sum(item["point_count"] for item in result), 3)
        self.assertTrue(next(item for item in result if item["map_id"] == "map-two")["truncated"])
        self.assertEqual(snapshot(self.path, self.robot)["sequence"], 5)

    def test_sequence_advances_by_batch_samples_not_messages(self):
        self.relocate()
        self.send(
            "livemap/update",
            {
                "pos_update": {"cur_path": [1, -0.9, -1.85, 0, 0, -0.8, -1.85, 0, 5, 1000]},
                "update_expire_ts": 1060,
            },
        )
        self.position(3)
        points = overlays(self.path, self.robot)["map-one"]["points"]
        self.assertEqual([point[3] for point in points], [True, False, False])
        self.position(5)
        self.assertTrue(overlays(self.path, self.robot)["map-one"]["points"][-1][3])

    def test_paused_last_known_and_non_cleaning_fresh_positions(self):
        self.relocate()
        self.position()
        for phase in (
            "hmUsrDock",
            "hmUsrChrg",
            "hmMidMsn",
            "hmPostMsn",
            "travel",
            "pause",
            "stuck",
        ):
            self.report_mission(1, "one", phase)
            self.assertEqual(overlays(self.path, self.robot)["map-one"]["status"], "live")
        self.report_mission(1, "one", "pause")
        with patch("time.time", return_value=1011):
            item = overlays(self.path, self.robot)["map-one"]
            self.assertEqual(item["status"], "last_known")
            self.assertEqual(item["position"]["x"], -0.9)
            self.assertIsNone(snapshot(self.path, self.robot))
        self.report_mission(1, "one", "idle")
        self.assertEqual(overlays(self.path, self.robot)["map-one"]["status"], "unavailable")
        with patch("time.time", return_value=1200):
            self.report_mission(1, "one", "charge")
            self.assertEqual(overlays(self.path, self.robot)["map-one"]["status"], "unavailable")

    def test_trail_pixels_breaks_and_docked_marker(self):
        model = decode_geometry(geometry(dock=False))
        trail = ((-0.9, -1.85, 0, True), (-0.7, -1.85, 5, False))
        with Image.open(io.BytesIO(render_png(model, size=256, trail=trail))) as image:
            self.assertLess(
                min(max(abs(c - 210) for c in image.getpixel((100, y))) for y in range(149, 153)),
                30,
            )
        broken = (trail[0], (-0.7, -1.85, 5, True))
        with Image.open(io.BytesIO(render_png(model, size=256, trail=broken))) as image:
            self.assertEqual(image.getpixel((100, 151)), (50, 105, 210))
        docked = decode_geometry(geometry())
        with Image.open(
            io.BytesIO(render_png(docked, size=256, position_status="docked"))
        ) as image:
            self.assertEqual(image.getpixel((74, 146)), (240, 240, 240))
        for trail in (((float("nan"), 0, 0, True),), ((0, 0, 5, "bad"),)):
            with self.assertRaises(ValueError):
                render_png(model, trail=trail)
