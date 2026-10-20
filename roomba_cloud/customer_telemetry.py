"""Build the versioned customer/HA contract from allowlisted local reports.

Never expose a raw shadow: fields/types come from ha_mqtt_publish plus the
explicit diagnostic additions here. Missing data is absent/unavailable rather
than fabricated zero/false. online reflects MQTT leases; last_data_received
tracks publications, with an old-database shadow-timestamp fallback.

map_inventory is authoritative existence, independent of decoded maps:
None=unknown (HA must not delete entities), []=known empty (HA may remove its
owned map images). Missing/pruned geometry does not imply deletion. Retained
archives cannot resurrect an ID absent from inventory. The companion integration
removes only its own absent map entities, including legacy room-map images; it
keeps stable Map N labels separately and exposes localized map_id as an attribute.

localized_map requires online, fresh mission-scoped live position and inventory
membership. It is intentionally unavailable while docked/expired/unlocalized,
not a guess based on archive order or last seen map. No authoritative idle-time
loaded-map signal or native map-selection dropdown has been established.

segment_catalog_complete separates a partial room archive catalog from inventory
deletion; settings/cleaning modes are capability-filtered. schema_version remains
1 for these additive fields, preserving older clients that ignore extra keys.
"""

import time
import contextlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from os import PathLike
from typing import NotRequired, TypedDict

from roomba_cloud.ha_mqtt_publish import (
    FIELDS,
    discovery,
    telemetry,
)

EXTRA_FIELDS = {
    "stats_mission_canceled_count": ("ro-stats", ("bbmssn", "nMssnC"), int, "Missions canceled"),
    "system_powered_hours": ("ro-stats", ("bbsys", "hr"), int, "Powered-on time"),
    "runtime_hours_raw": (
        "ro-currentstate",
        ("runtimeStats", "hr"),
        int,
        "Lifetime runtime raw counter",
    ),
    "runtime_minutes_raw": (
        "ro-currentstate",
        ("runtimeStats", "min"),
        int,
        "Lifetime runtime minute component raw",
    ),
}
CUSTOMER_FIELDS = {**FIELDS, **EXTRA_FIELDS}


class TelemetryField(TypedDict):
    key: str
    name: str
    kind: str
    device_class: NotRequired[str]
    unit_of_measurement: NotRequired[str]
    state_class: NotRequired[str]
    entity_category: NotRequired[str]


class DNSChallenge(TypedDict):
    name: str
    type: str
    value: str


def dns_challenge(path: str | PathLike[str] | None) -> DNSChallenge | None:
    if path is None:
        return None
    try:
        raw = Path(path).read_text()
    except FileNotFoundError:
        return None
    document = json.loads(raw)
    if (
        not isinstance(document, dict)
        or set(document) != {"name", "type", "value"}
        or document["type"] != "TXT"
        or not isinstance(document["name"], str)
        or len(document["name"]) > 269
        or not re.fullmatch(r"_acme-challenge\.[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", document["name"])
        or not isinstance(document["value"], str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{43}", document["value"])
    ):
        raise ValueError("Invalid ACME DNS challenge")
    return {"name": document["name"], "type": "TXT", "value": document["value"]}


class MapDescriptor(TypedDict):
    id: str
    version: str
    revision: str


class SegmentDescriptor(TypedDict):
    id: str
    name: str
    group: str


class Position(TypedDict):
    x: float
    y: float
    heading: float
    timestamp: float
    sequence: int
    received_at: float
    map_id: str
    expires_at: float


class OverlayDescriptor(TypedDict):
    map_id: str
    revision: int
    status: str
    point_count: int
    truncated: bool


class SettingDescriptor(TypedDict):
    key: str
    name: str
    kind: str
    value: bool | int
    options: dict[str, str]


class TelemetrySnapshot(TypedDict):
    """Customer wire shape; typed producers are still being adopted incrementally.

    Optional inventory/position are semantic unknowns, not empty successful
    fallbacks. Field availability is separate from robot online status.
    """

    schema_version: int
    robot_id: str
    reported_at: int | None
    online: bool
    values: dict[str, str | int | float | bool]
    available: dict[str, bool]
    fields: list[TelemetryField]
    segments: list[SegmentDescriptor]
    segment_colors: dict[str, str]
    maps: list[MapDescriptor]
    map_inventory: list[str] | None
    position: Position | None
    map_overlays: list[OverlayDescriptor]
    segment_catalog_complete: bool
    settings: list[SettingDescriptor]
    cleaning_modes: list[int]
    device: dict[str, str]
    acme_dns_challenge: DNSChallenge | None


def snapshot(
    path: str | PathLike[str],
    robot_id: str,
    map_upload_db: str | PathLike[str] | None = None,
    acme_challenge_file: str | PathLike[str] | None = None,
) -> TelemetrySnapshot | None:
    """Return None only when no base telemetry exists; propagate storage/decode errors.

    Keep known-empty inventory in the response even when there are no map
    descriptors or room targets. Geometry/cache errors must reach HTTP as errors,
    never as deletion-shaped success data.
    """
    values, timestamps, reported_at = telemetry(path, robot_id, CUSTOMER_FIELDS)
    if reported_at is None and not values:
        return None
    now = time.time()
    with contextlib.closing(
        sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    ) as db:
        db.execute("BEGIN")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        connected = None
        if "mqtt_sessions" in tables:
            connected = bool(
                db.execute(
                    "SELECT 1 FROM mqtt_sessions WHERE robot_id=? AND expires>=? LIMIT 1",
                    (robot_id, now),
                ).fetchone()
            )
        row = (
            db.execute(
                "SELECT received_at FROM robot_data WHERE robot_id=?", (robot_id,)
            ).fetchone()
            if "robot_data" in tables
            else None
        )
        last_data = row[0] if row else None
        base = db.execute(
            "SELECT document FROM shadows WHERE topic=?",
            (f"$aws/things/{robot_id}/shadow",),
        ).fetchone()
        reported = json.loads(base[0]).get("state", {}).get("reported", {}) if base else {}
        device = {}
        for source, target in (("sku", "model"), ("serialNum", "serial_number")):
            value = reported.get(source)
            if (
                isinstance(value, str)
                and 1 <= len(value) <= 128
                and not any(ord(char) < 32 for char in value)
            ):
                device[target] = value
        firmware = values.get("firmware_version")
        if (
            isinstance(firmware, str)
            and 1 <= len(firmware) <= 128
            and not any(ord(char) < 32 for char in firmware)
        ):
            device["sw_version"] = firmware
        if last_data is None:
            # Existing databases can still expose their latest persisted shadow update.
            last_data = max(
                (
                    json_time
                    for (raw,) in db.execute(
                        "SELECT document FROM shadows WHERE topic LIKE ?",
                        (f"$aws/things/{robot_id}/shadow/%",),
                    )
                    if type(json_time := json.loads(raw).get("timestamp")) is int
                ),
                default=None,
            )
    fields: list[TelemetryField] = []
    for key, (_, descriptor) in zip(FIELDS, discovery(robot_id, ""), strict=True):
        fields.append(
            {
                "key": key,
                "name": descriptor["name"],
                "kind": "binary_sensor" if FIELDS[key][2] is bool else "sensor",
            }
        )
        if "device_class" in descriptor:
            fields[-1]["device_class"] = descriptor["device_class"]
        if "unit_of_measurement" in descriptor:
            fields[-1]["unit_of_measurement"] = descriptor["unit_of_measurement"]
        if "state_class" in descriptor:
            fields[-1]["state_class"] = descriptor["state_class"]
        if "entity_category" in descriptor:
            fields[-1]["entity_category"] = descriptor["entity_category"]
    for key, (_, _, _, name) in EXTRA_FIELDS.items():
        extra: TelemetryField = {
            "key": key,
            "name": name,
            "kind": "sensor",
            "entity_category": "diagnostic",
        }
        if key == "stats_mission_canceled_count":
            extra["state_class"] = "total_increasing"
        elif key == "system_powered_hours":
            extra["device_class"] = "duration"
            extra["unit_of_measurement"] = "h"
            extra["state_class"] = "total_increasing"
        fields.append(extra)
    fields.extend(
        [
            {
                "key": "robot_mqtt_connection",
                "name": "Robot MQTT connection",
                "kind": "binary_sensor",
                "device_class": "connectivity",
                "entity_category": "diagnostic",
            },
            {
                "key": "last_data_received",
                "name": "Last data received",
                "kind": "sensor",
                "device_class": "timestamp",
                "entity_category": "diagnostic",
            },
            {
                "key": "localized_map",
                "name": "Localized map",
                "kind": "sensor",
                "entity_category": "diagnostic",
            },
        ]
    )
    if connected is not None:
        values["robot_mqtt_connection"] = connected
    if last_data is not None:
        values["last_data_received"] = datetime.fromtimestamp(last_data, timezone.utc).isoformat()
    from roomba_cloud.maps.catalog import map_segments, saved_maps, reported_maps
    from roomba_cloud.maps.customer_maps import descriptors
    from roomba_cloud.maps.live_position import (
        snapshot as live_snapshot,
        descriptors as overlay_descriptors,
    )
    from roomba_cloud.maps.preview import room_colors
    from roomba_cloud.robot_settings import cleaning_modes, configuration

    maps = saved_maps(path, map_upload_db, robot_id)
    inventory = reported_maps(path, robot_id)
    if inventory is not None:
        maps = {identifier: item for identifier, item in maps.items() if identifier in inventory}
    position = live_snapshot(path, robot_id)
    if connected and position and inventory is not None and position["map_id"] in inventory:
        values["localized_map"] = position["map_id"]
    segments = map_segments(maps)
    colors = {map_id: room_colors(item["rooms"]) for map_id, item in maps.items()}
    return {
        "schema_version": 1,
        "robot_id": robot_id,
        "reported_at": reported_at,
        "online": connected is True,
        "values": values,
        "available": {field["key"]: field["key"] in values for field in fields},
        "fields": fields,
        "segments": [
            {"id": segment["id"], "name": segment["name"], "group": segment["group"]}
            for segment in segments
        ],
        "segment_colors": {
            segment["id"]: colors[segment["map_id"]][segment["region_id"]][1]
            for segment in segments
        },
        "maps": descriptors(maps),
        "map_inventory": sorted(inventory) if inventory is not None else None,
        "position": position,
        "map_overlays": overlay_descriptors(path, robot_id),
        "segment_catalog_complete": (
            inventory is not None and set(inventory) == {segment["map_id"] for segment in segments}
        ),
        "settings": configuration(path, robot_id),
        "cleaning_modes": cleaning_modes(path, robot_id),
        "device": device,
        "acme_dns_challenge": dns_challenge(acme_challenge_file),
    }
