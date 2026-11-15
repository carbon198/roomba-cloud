import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from roomba_cloud.customer_telemetry import CUSTOMER_FIELDS
from roomba_cloud.shadow_service import ShadowService
from roomba_cloud.telemetry_schema import field_descriptor, telemetry


class TelemetrySchemaTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "shadows.db"
        self.service = ShadowService(self.path)
        self.robot_id = "a" * 32

    def report(self, name, value):
        self.service.respond(
            f"$aws/things/{self.robot_id}/shadow/name/{name}/update",
            json.dumps({"state": {"reported": value}}).encode(),
            self.robot_id,
        )

    def test_extracts_only_allowlisted_values_with_exact_types_and_timestamps(self):
        with patch("roomba_cloud.shadow_service.time.time", return_value=100):
            self.report(
                "ro-currentstate",
                {
                    "batPct": 72,
                    "bin": {"present": True},
                    "cleanMissionStatus": {
                        "phase": "charge",
                        "sqft": 36,
                        "nMssn": 121,
                        "condNotReady": ["private"],
                        "mssnStrtTm": 1792008993,
                    },
                    "p2maps": [{"p2map_id": "private-map"}],
                    "password": "private-password",
                },
            )
            self.report("rw-settings", {"childLock": False, "suctionLevel": 3})
            self.report(
                "rw-software",
                {"softwareVer": "firmware-test", "lastCommand": {"time": 1792008992}},
            )

        data, timestamps, reported_at = telemetry(self.path, self.robot_id)
        self.assertEqual(
            data,
            {
                "battery": 72,
                "mission_phase": "charge",
                "bin_present": True,
                "suction_level": 3,
                "child_lock": False,
                "mission_area": 36,
                "mission_count": 121,
                "mission_started": "2026-10-14T20:16:33+00:00",
                "not_ready_conditions": 1,
                "map_count": 1,
                "firmware_version": "firmware-test",
                "last_command_time": "2026-10-14T20:16:32+00:00",
            },
        )
        self.assertEqual(set(timestamps), set(data))
        self.assertEqual(reported_at, 100)
        self.assertNotIn("private", json.dumps(data))
        self.assertEqual(telemetry(self.path, "b" * 32), ({}, {}, None))

    def test_invalid_types_counts_timestamps_and_error_details_are_omitted(self):
        self.report(
            "ro-currentstate",
            {
                "batPct": True,
                "p2maps": {"private": 1},
                "cleanMissionStatus": {
                    "sqft": True,
                    "nMssn": -1,
                    "mssnStrtTm": 0,
                    "condNotReady": "private",
                },
            },
        )
        self.report("rw-software", {"lastCommand": {"time": 253402300800}})
        self.report("ro-stats", {"unprocessedError": "valid diagnostic"})
        values, _, _ = telemetry(self.path, self.robot_id, CUSTOMER_FIELDS)
        self.assertNotIn("battery", values)
        self.assertNotIn("mission_area", values)
        self.assertNotIn("mission_count", values)
        self.assertNotIn("mission_started", values)
        self.assertNotIn("not_ready_conditions", values)
        self.assertNotIn("map_count", values)
        self.assertNotIn("last_command_time", values)
        self.assertEqual(values["stats_unprocessed_error"], "valid diagnostic")

        self.report("ro-stats", {"unprocessedError": "x" * 257})
        values, _, _ = telemetry(self.path, self.robot_id, CUSTOMER_FIELDS)
        self.assertNotIn("stats_unprocessed_error", values)

    def test_field_descriptors_preserve_ha_metadata_without_mqtt_topics(self):
        self.assertEqual(
            field_descriptor("battery"),
            {
                "name": "Battery",
                "kind": "sensor",
                "device_class": "battery",
                "unit_of_measurement": "%",
                "state_class": "measurement",
            },
        )
        self.assertEqual(
            field_descriptor("child_lock"),
            {"name": "Child lock", "kind": "binary_sensor"},
        )
        self.assertEqual(
            field_descriptor("mission_started")["device_class"],
            "timestamp",
        )
        self.assertEqual(
            field_descriptor("mission_area")["unit_of_measurement"],
            "ft\u00b2",
        )
        self.assertEqual(
            field_descriptor("charge_success_count")["entity_category"],
            "diagnostic",
        )
