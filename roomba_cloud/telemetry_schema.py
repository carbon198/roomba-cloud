"""Shared allowlist and extraction helpers for robot-reported telemetry.

The field schema is used by the customer API and Home Assistant entity
descriptors. Raw shadows and unknown/private fields are never exposed.
"""

import contextlib
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import NotRequired, TypedDict


class FieldDescriptor(TypedDict):
    name: str
    kind: str
    device_class: NotRequired[str]
    unit_of_measurement: NotRequired[str]
    state_class: NotRequired[str]
    entity_category: NotRequired[str]


FIELDS = {
    "battery": ("ro-currentstate", ("batPct",), int, "Battery"),
    "mission_phase": ("ro-currentstate", ("cleanMissionStatus", "phase"), str, "Mission phase"),
    "mission_cycle": ("ro-currentstate", ("cleanMissionStatus", "cycle"), str, "Mission cycle"),
    "mission_error": (
        "ro-currentstate",
        ("cleanMissionStatus", "error"),
        int,
        "Mission error code",
    ),
    "bin_present": ("ro-currentstate", ("bin", "present"), bool, "Bin present"),
    "tank_present": ("ro-currentstate", ("tankPresent",), bool, "Tank present"),
    "detected_pad": ("ro-currentstate", ("detectedPad",), str, "Detected pad"),
    "suction_level": ("rw-settings", ("suctionLevel",), int, "Suction level"),
    "pad_wetness": ("rw-settings", ("padWetness", "padPlate"), int, "Pad wetness"),
    "carpet_boost": ("rw-settings", ("carpetBoost",), bool, "Carpet boost"),
    "child_lock": ("rw-settings", ("childLock",), bool, "Child lock"),
    "evacuation_allowed": ("rw-settings", ("evacAllowed",), bool, "Evacuation allowed"),
    "mission_area": (
        "ro-currentstate",
        ("cleanMissionStatus", "sqft"),
        int,
        "Mission cleaned area",
    ),
    "mission_count": (
        "ro-currentstate",
        ("cleanMissionStatus", "nMssn"),
        int,
        "Mission sequence count",
    ),
    "mission_initiator": (
        "ro-currentstate",
        ("cleanMissionStatus", "initiator"),
        str,
        "Mission initiator",
    ),
    "mission_started": (
        "ro-currentstate",
        ("cleanMissionStatus", "mssnStrtTm"),
        int,
        "Mission started",
    ),
    "not_ready_code": (
        "ro-currentstate",
        ("cleanMissionStatus", "notReady"),
        int,
        "Not-ready code",
    ),
    "not_ready_conditions": (
        "ro-currentstate",
        ("cleanMissionStatus", "condNotReady"),
        list,
        "Not-ready condition count",
    ),
    "operating_mode": (
        "ro-currentstate",
        ("cleanMissionStatus", "operatingMode"),
        int,
        "Operating mode raw code",
    ),
    "map_count": ("ro-currentstate", ("p2maps",), list, "Map reference count"),
    "dock_known": ("ro-currentstate", ("dock", "known"), bool, "Dock known"),
    "dock_state": ("ro-currentstate", ("dock", "state"), int, "Dock state raw code"),
    "dock_pad_dry_state": ("ro-currentstate", ("dock", "pdState"), int, "Dock pad drying raw code"),
    "dock_pad_wash_state": (
        "ro-currentstate",
        ("dock", "pwState"),
        int,
        "Dock pad washing raw code",
    ),
    "dock_error": ("ro-currentstate", ("dock", "error"), int, "Dock error code"),
    "dock_firmware_version": ("ro-currentstate", ("dock", "fwVer"), str, "Dock firmware version"),
    "audio_volume": ("rw-settings", ("audio", "volume"), int, "Audio volume raw level"),
    "eco_charge": ("rw-settings", ("ecoCharge",), bool, "Eco charging"),
    "map_upload_allowed": ("rw-settings", ("mapUploadAllowed",), bool, "Map upload allowed"),
    "two_pass": ("rw-settings", ("twoPass",), bool, "Two-pass cleaning"),
    "no_auto_passes": ("rw-settings", ("noAutoPasses",), bool, "Automatic passes disabled"),
    "schedule_hold": ("rw-settings", ("schedHold",), bool, "Schedule hold"),
    "auto_evac_frequency": (
        "rw-settings",
        ("autoevacFreq",),
        int,
        "Auto evacuation frequency raw code",
    ),
    "pad_dry_allowed": ("rw-settings", ("padDryAllowed",), int, "Pad drying allowed raw code"),
    "pad_wash_allowed": ("rw-settings", ("padWashAllowed",), int, "Pad washing allowed raw code"),
    "charge_success_count": ("ro-stats", ("bbchg", "nChgOk"), int, "Successful charge count"),
    "charge_error_count": ("ro-stats", ("bbchg", "nChgErr"), int, "Charge error count"),
    "stats_mission_count": ("ro-stats", ("bbmssn", "nMssn"), int, "Statistics mission count"),
    "stats_mission_success_count": (
        "ro-stats",
        ("bbmssn", "nMssnOk"),
        int,
        "Successful mission count",
    ),
    "stats_mission_failure_count": ("ro-stats", ("bbmssn", "nMssnF"), int, "Failed mission count"),
    "navigation_reset_count": ("ro-stats", ("bbrstinfo", "nNavRst"), int, "Navigation reset count"),
    "firmware_version": ("rw-software", ("softwareVer",), str, "Firmware version"),
    "connectivity_firmware_version": (
        "rw-software",
        ("subModSwVer", "con"),
        str,
        "Connectivity firmware version",
    ),
    "navigation_firmware_version": (
        "rw-software",
        ("subModSwVer", "nav"),
        str,
        "Navigation firmware version",
    ),
    "mcu_firmware_version": ("rw-software", ("subModSwVer", "mcu"), str, "MCU firmware version"),
    "last_command": ("rw-software", ("lastCommand", "command"), str, "Last command"),
    "last_command_initiator": (
        "rw-software",
        ("lastCommand", "initiator"),
        str,
        "Last command initiator",
    ),
    "last_command_time": ("rw-software", ("lastCommand", "time"), int, "Last command time"),
}

TIMESTAMP_FIELDS = {"mission_started", "last_command_time"}
COUNT_FIELDS = {key for key in FIELDS if key.endswith("_count")} | {"not_ready_conditions"}


def lookup(data, keys):
    for key in keys:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def telemetry(db_path, robot_id, fields=None):
    """Read a consistent shadow snapshot and return sanitized values and timestamps."""
    fields = FIELDS if fields is None else fields
    prefix = f"$aws/things/{robot_id}/shadow/name/"
    with contextlib.closing(
        sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
    ) as db:
        documents = {}
        db.execute("BEGIN")
        for name in dict.fromkeys(field[0] for field in fields.values()):
            row = db.execute(
                "SELECT document FROM shadows WHERE topic=?", (prefix + name,)
            ).fetchone()
            documents[name] = json.loads(row[0]) if row else {}
    result, times = {}, {}
    for key, (name, keys, expected, _) in fields.items():
        document = documents[name]
        value = lookup(document.get("state", {}).get("reported", {}), keys)
        if type(value) is expected:
            if key == "stats_unprocessed_error" and (
                len(value) > 256 or any(ord(char) < 32 for char in value)
            ):
                continue
            if key == "battery" and not 0 <= value <= 100:
                continue
            if expected is list:
                value = len(value)
            if (key in COUNT_FIELDS or key.endswith("_count")) and value < 0:
                continue
            if key in TIMESTAMP_FIELDS:
                if not 0 < value <= 253402300799:
                    continue
                value = datetime.fromtimestamp(value, timezone.utc).isoformat()
            result[key] = value
            timestamp = lookup(
                document.get("metadata", {}).get("reported", {}), keys + ("timestamp",)
            )
            if type(timestamp) is int:
                times[key] = timestamp
    reported_at = documents["ro-currentstate"].get("timestamp")
    return result, times, reported_at if type(reported_at) is int else None


def field_descriptor(key: str) -> FieldDescriptor:
    """Return shared Home Assistant field metadata without transport/discovery topics."""
    _, _, expected, label = FIELDS[key]
    descriptor = {
        "name": label,
        "kind": "binary_sensor" if expected is bool else "sensor",
    }
    if key == "battery":
        descriptor.update(
            device_class="battery", unit_of_measurement="%", state_class="measurement"
        )
    if key == "mission_area":
        descriptor.update(unit_of_measurement="ft\u00b2", state_class="measurement")
    if key in TIMESTAMP_FIELDS:
        descriptor["device_class"] = "timestamp"
    if key in COUNT_FIELDS:
        descriptor["state_class"] = "measurement"
    if (
        key
        in (
            "suction_level",
            "pad_wetness",
            "mission_error",
            "not_ready_code",
            "operating_mode",
            "dock_state",
            "dock_pad_dry_state",
            "dock_pad_wash_state",
            "dock_error",
            "auto_evac_frequency",
            "pad_dry_allowed",
            "pad_wash_allowed",
            "audio_volume",
        )
        or key == "dock_firmware_version"
        or FIELDS[key][0] in ("ro-stats", "rw-software")
    ):
        descriptor["entity_category"] = "diagnostic"
    return descriptor
