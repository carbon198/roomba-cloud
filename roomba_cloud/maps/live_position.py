"""Persist mission-scoped position/trails without inventing live localization.

Accept only this robot's mission/timeline/report and livemap/update. Relocation
events bind map identity to the current (nMssn, missionId); cur_path supplies
sequence, repeated x/y/heading/activity samples, then timestamp. World positions
are not pixel coordinates. Activity bits are retained, not assigned guessed
cleaning meanings. The total trail budget is 12,000 points across maps.

New missions reset old state; sequence/time gaps and relocation break lines.
Fresh means received within ten seconds and not past update_expire_ts, with a
live MQTT lease and matching mission. Pause/stop/stuck/error may retain an
explicit last_known pose; charging may use the saved dock, never stale live pose.
Completed trails persist for display but are not proof of cleaning coverage.
The customer Localized map sensor uses only status=live; it is NOT a reliable
idle-time loaded-map sensor. Map existence still comes from p2maps inventory.
"""

import contextlib
import json
import math
from pathlib import Path
import sqlite3
import time

from roomba_cloud.maps.catalog import IDENTIFIER

MAX_TRAIL_POINTS = 12_000
UNDOCKED_PHASES = frozenset(
    (
        "run",
        "resume",
        "hmUsrDock",
        "hmUsrChrg",
        "hmMidMsn",
        "hmPostMsn",
        "pause",
        "stop",
        "stuck",
        "error",
        "room",
        "zone",
        "polygon",
        "oClean",
        "travel",
        "traversal",
        "reloc",
        "explore",
        "plan",
    )
)


def identity(current):
    """Use both mission counters/IDs; phase changes alone do not identify a new mission."""
    return [current.get("nMssn"), current.get("missionId")]


def reset_mission(db, robot_id, current):
    """Reset in the caller's shadow transaction, only for a valid undocked mission."""
    mission_id = identity(current)
    if (
        current.get("phase") not in UNDOCKED_PHASES
        or type(mission_id[0]) is not int
        or not isinstance(mission_id[1], str)
        or not mission_id[1]
    ):
        return
    row = db.execute("SELECT document FROM live_positions WHERE robot_id=?", (robot_id,)).fetchone()
    if row:
        previous = json.loads(row[0])
        if previous.get("mission") != mission_id:
            db.execute(
                "UPDATE live_positions SET document=? WHERE robot_id=?",
                (
                    json.dumps(
                        {"mission": mission_id, "revision": previous.get("revision", 0) + 1}
                    ),
                    robot_id,
                ),
            )


def mission(db, robot_id):
    row = db.execute(
        "SELECT document FROM shadows WHERE topic=?",
        (f"$aws/things/{robot_id}/shadow/name/ro-currentstate",),
    ).fetchone()
    return (
        json.loads(row[0]).get("state", {}).get("reported", {}).get("cleanMissionStatus", {})
        if row
        else {}
    )


def ingest(path, robot_id, topic, payload):
    """Validate/bound telemetry and persist only current-mission observations.

    Ignore unrelated topics or observations lacking a verified mission, rather
    than merging them with previous state. Reject malformed relevant payloads so
    the MQTT caller can report them explicitly without raw private geometry.
    """
    prefix = f"v011-irbthbu/things/{robot_id}/"
    if topic not in (prefix + "mission/timeline/report", prefix + "livemap/update"):
        return
    if len(payload) > 256 * 1024:
        raise ValueError("Live telemetry exceeds size limit")
    data = json.loads(payload)
    if not isinstance(data, dict):
        raise ValueError("Invalid live telemetry envelope")
    now = time.time()
    with contextlib.closing(sqlite3.connect(path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        current = mission(db, robot_id)
        mission_id = identity(current)
        if (
            type(mission_id[0]) is not int
            or not isinstance(mission_id[1], str)
            or not mission_id[1]
        ):
            return
        row = db.execute(
            "SELECT document FROM live_positions WHERE robot_id=?", (robot_id,)
        ).fetchone()
        state = json.loads(row[0]) if row else {}
        if current.get("phase") not in UNDOCKED_PHASES:
            return
        if state.get("mission") != mission_id:
            state = {"mission": mission_id, "revision": state.get("revision", 0) + 1}
        if topic.endswith("mission/timeline/report"):
            if [data.get("nMssn"), data.get("mission_id")] != mission_id:
                return
            if any(not isinstance(data.get(key, []), list) for key in ("finEvents", "event")):
                raise ValueError("Invalid mission timeline events")
            events = data.get("finEvents", []) + data.get("event", [])
            if len(events) > 1000:
                raise ValueError("Oversized mission timeline")
            relocations = [
                event
                for event in events
                if isinstance(event, dict)
                and event.get("type") == "reloc"
                and isinstance(event.get("reloc"), dict)
            ]
            if relocations:
                if any(type(event.get("ts")) is not int for event in relocations):
                    raise ValueError("Invalid relocation timestamp")
                event = max(relocations, key=lambda event: event["ts"])
                if event["ts"] < state.get("relocated_at", 0):
                    return
                map_id = event["reloc"].get("p2mapId")
                if not isinstance(map_id, str) or not IDENTIFIER.fullmatch(map_id):
                    raise ValueError("Invalid live map identity")
                if state.get("map_id") != map_id:
                    state.pop("position", None)
                    state["break_trail"] = True
                state["map_id"] = map_id
                state["relocated_at"] = event["ts"]
        elif "pos_update" in data:
            update = data["pos_update"]
            path_data = update.get("cur_path") if isinstance(update, dict) else None
            if (
                not isinstance(path_data, list)
                or not 6 <= len(path_data) <= 4002
                or (len(path_data) - 2) % 4
                or any(type(v) not in (int, float) or not math.isfinite(v) for v in path_data)
                or type(path_data[0]) is not int
                or path_data[0] < 0
            ):
                raise ValueError("Invalid live position batch")
            timestamp = path_data[-1]
            expiry = data.get("update_expire_ts")
            if (
                abs(now - timestamp) > 10
                or type(expiry) not in (int, float)
                or not math.isfinite(expiry)
                or not now < expiry <= now + 600
            ):
                raise ValueError("Invalid live position timestamp/expiry")
            previous = state.get("position")
            if (
                previous
                and timestamp <= previous["timestamp"]
                and path_data[0] <= previous["sequence"]
            ):
                return
            samples = [path_data[i : i + 4] for i in range(1, len(path_data) - 1, 4)]
            for x, y, heading, mode in samples:
                if (
                    abs(x) > 1_000_000
                    or abs(y) > 1_000_000
                    or abs(heading) > 2 * math.pi
                    or type(mode) is not int
                    or not 0 <= mode < 2**32
                ):
                    raise ValueError("Invalid live position coordinate or activity")
            x, y, heading, _ = samples[-1]
            map_id = state.get("map_id")
            if map_id:
                trails = state.setdefault("trails", {})
                if map_id not in trails and len(trails) >= 10:
                    raise ValueError("Mission trail exceeds map limit")
                trail = trails.setdefault(map_id, {"points": [], "revision": 0, "truncated": False})
                gap = (
                    state.pop("break_trail", False)
                    or previous is None
                    or now - previous["received_at"] > 10
                    or path_data[0] != state.get("next_sequence", previous["sequence"] + 1)
                )
                remaining = MAX_TRAIL_POINTS - sum(len(item["points"]) for item in trails.values())
                for index, (x, y, heading, mode) in enumerate(samples):
                    if remaining <= 0:
                        trail["truncated"] = True
                        break
                    trail["points"].append([x, y, mode, bool(gap and index == 0)])
                    remaining -= 1
                state["revision"] = state.get("revision", 0) + 1
                trail["revision"] = state["revision"]
            x, y, heading, _ = samples[-1]
            state["position"] = {
                "x": x,
                "y": y,
                "heading": heading,
                "timestamp": timestamp,
                "sequence": path_data[0],
                "received_at": now,
            }
            state["expires_at"] = expiry
            state["next_sequence"] = path_data[0] + len(samples)
        else:
            return
        db.execute(
            "INSERT OR REPLACE INTO live_positions VALUES (?,?)",
            (robot_id, json.dumps(state, allow_nan=False)),
        )


def overlays(path, robot_id):
    """Derive live/last_known/docked/unavailable without erasing retained trail evidence."""
    with contextlib.closing(
        sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    ) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='live_positions'").fetchone():
            return {}
        row = db.execute(
            "SELECT document FROM live_positions WHERE robot_id=?", (robot_id,)
        ).fetchone()
        if not row:
            return {}
        state = json.loads(row[0])
        current = mission(db, robot_id)
        now = time.time()
        online = db.execute(
            "SELECT 1 FROM mqtt_sessions WHERE robot_id=? AND expires>=?", (robot_id, now)
        ).fetchone()
    phase = current.get("phase")
    same_mission = state.get("mission") == identity(current)
    if not same_mission and phase in UNDOCKED_PHASES:
        return {}
    result = {
        map_id: {**trail, "status": "unavailable", "position": None}
        for map_id, trail in state.get("trails", {}).items()
    }
    map_id = state.get("map_id")
    if not map_id:
        return result
    item = result.setdefault(
        map_id,
        {
            "points": [],
            "revision": 0,
            "truncated": False,
            "status": "unavailable",
            "position": None,
        },
    )
    position = state.get("position")
    if online and phase == "charge":
        item["status"] = "docked"
    elif online and same_mission and position and phase in UNDOCKED_PHASES:
        fresh = 0 <= now - position["received_at"] <= 10 and state.get("expires_at", 0) > now
        if fresh:
            item["status"] = "live"
        elif phase in ("pause", "stop", "stuck", "error"):
            item["status"] = "last_known"
        if item["status"] != "unavailable":
            item["position"] = {
                **position,
                "map_id": map_id,
                "expires_at": state.get("expires_at", 0),
            }
    return result


def descriptors(path, robot_id):
    return [
        {
            "map_id": map_id,
            "revision": item["revision"],
            "status": item["status"],
            "point_count": len(item["points"]),
            "truncated": item["truncated"],
        }
        for map_id, item in sorted(overlays(path, robot_id).items())
    ]


def snapshot(path, robot_id):
    """Expose only a fresh localized pose; dock and last-known substitutes are not live."""
    return next(
        (
            item["position"]
            for item in overlays(path, robot_id).values()
            if item["status"] == "live"
        ),
        None,
    )
