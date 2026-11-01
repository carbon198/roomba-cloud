"""Persist validated, robot-scoped dock operation reports from MQTT."""

import contextlib
import json
import math
import sqlite3
import time
from os import PathLike

from roomba_cloud.shadow_service import finite_float, reject_constant

REPORT_TOPICS = {
    "evacuation": "evac/report",
    "refill": "dock/refill/report",
    "padwash": "dock/padwash/report",
    "paddry": "dock/paddry/report",
}
MAX_REPORT_BYTES = 64 * 1024
MAX_REPORT_DEPTH = 8
MAX_REPORT_ITEMS = 512
MAX_REPORT_STRING_LENGTH = 4096


class DockReportError(ValueError):
    """A bounded dock report payload or storage error suitable for safe logging."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DockReportError("duplicate_json_key")
        result[key] = value
    return result


def _validate_value(value, depth=0, items=None):
    if items is None:
        items = [0]
    items[0] += 1
    if depth > MAX_REPORT_DEPTH:
        raise DockReportError("report_nesting_exceeds_limit")
    if items[0] > MAX_REPORT_ITEMS * 8:
        raise DockReportError("report_item_count_exceeds_limit")
    if isinstance(value, dict):
        if len(value) > MAX_REPORT_ITEMS or any(
            not isinstance(key, str) or len(key) > 128 or any(ord(char) < 32 for char in key)
            for key in value
        ):
            raise DockReportError("invalid_report_object")
        for item in value.values():
            _validate_value(item, depth + 1, items)
    elif isinstance(value, list):
        if len(value) > MAX_REPORT_ITEMS:
            raise DockReportError("report_array_exceeds_limit")
        for item in value:
            _validate_value(item, depth + 1, items)
    elif isinstance(value, str):
        if len(value) > MAX_REPORT_STRING_LENGTH:
            raise DockReportError("report_string_exceeds_limit")
    elif type(value) is float:
        if not math.isfinite(value):
            raise DockReportError("non_finite_report_number")
    elif value is not None and type(value) not in (int, bool):
        raise DockReportError("invalid_report_value")


def parse_report(payload):
    if not isinstance(payload, bytes) or not 0 < len(payload) <= MAX_REPORT_BYTES:
        raise DockReportError("invalid_report_size")
    try:
        report = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=reject_constant,
            parse_float=finite_float,
        )
    except DockReportError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise DockReportError("invalid_report_json") from exc
    if not isinstance(report, dict):
        raise DockReportError("report_must_be_an_object")
    _validate_value(report)
    return report


def store_report(
    database: str | PathLike[str], robot_id: str, report_type: str, payload: bytes
) -> int:
    """Keep the latest complete payload for this robot and report family."""
    if report_type not in REPORT_TOPICS:
        raise DockReportError("unsupported_report_type")
    report = parse_report(payload)
    encoded = json.dumps(report, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    received_at = time.time()
    with contextlib.closing(sqlite3.connect(database, timeout=10)) as db, db:
        db.execute(
            "INSERT INTO dock_reports(robot_id,report_type,document,received_at) "
            "VALUES (?,?,?,?) ON CONFLICT(robot_id,report_type) DO UPDATE SET "
            "document=excluded.document,received_at=excluded.received_at",
            (robot_id, report_type, encoded, received_at),
        )
    return received_at
