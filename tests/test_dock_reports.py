import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from roomba_cloud.customer_telemetry import snapshot
from roomba_cloud.dock_reports import DockReportError, parse_report, store_report
from roomba_cloud.shadow_service import ShadowService


class DockReportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "shadow.db"
        self.service = ShadowService(self.path)
        self.robot_id = "a" * 32
        self.service.respond(
            f"$aws/things/{self.robot_id}/shadow/name/ro-currentstate/update",
            b'{"state":{"reported":{"cleanMissionStatus":{"phase":"charge","cycle":"none"}}}}',
            self.robot_id,
        )

    def test_parse_report_preserves_unknown_and_nested_values(self):
        report = parse_report(
            b'{"reportType":"padDry","pdState":701,"bbk":{"totalPadDry":4},'
            b'"cap":{"pd":2},"futureField":[true,null,"x"]}'
        )
        self.assertEqual(report["bbk"]["totalPadDry"], 4)
        self.assertEqual(report["futureField"], [True, None, "x"])

    def test_parse_report_rejects_malformed_unbounded_or_ambiguous_json(self):
        for payload in (
            b"[]",
            b'{"error":NaN}',
            b'{"error":1,"error":2}',
            b'{"payload":"' + b"x" * (64 * 1024) + b'"}',
            b'{"nested":{"a":{"b":{"c":{"d":{"e":{"f":{"g":{"h":{"i":1}}}}}}}}}',
        ):
            with self.subTest(payload_length=len(payload)):
                with self.assertRaises(DockReportError):
                    parse_report(payload)

    def test_store_latest_report_per_robot_and_expose_in_customer_telemetry(self):
        payload = b'{"reportType":"padDry","reportTime":1700000000,"pdState":701,'
        payload += b'"bbk":{"totalPadDry":4,"totalPadDryTime":3600}}'
        with patch("roomba_cloud.dock_reports.time.time", return_value=1000.5):
            store_report(self.path, self.robot_id, "paddry", payload)
        result = snapshot(self.path, self.robot_id)
        self.assertEqual(
            result["dock_reports"]["paddry"],
            {
                "payload": json.loads(payload),
                "received_at": 1000.5,
            },
        )
        self.assertEqual(result["online"], False)
        self.assertIsNone(snapshot(self.path, "b" * 32))

    def test_store_rejects_unknown_report_family(self):
        with self.assertRaisesRegex(DockReportError, "unsupported_report_type"):
            store_report(self.path, self.robot_id, "battery", b"{}")
