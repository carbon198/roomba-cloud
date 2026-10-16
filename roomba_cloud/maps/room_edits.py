"""Robot-scoped map edits, upload recovery probes and durable edit locks.

Safety/lifecycle
---------------
Only known cycle="none" with charge/stop/idle phase permits user edits OR probes
(map_controls). Recheck the mission while constructing the MQTT payload.
Browser acknowledgements are UX, not a substitute for server checks. Publish
once: PUBACK is delivery, edit_resp is an application reply, fresh geometry or
inventory establishes the resulting map state. Never retry uncertain edits.
Firmware map loading can delete an unhealthy saved map; retained archives are
NOT a verified restore/undo mechanism.

Ordinary edits lock the exact robot/map until a completed upload contains
verified geometry, regardless of transfer kind, digest equality or catalog
version selection. Success/rejection/timeouts/reloads/restarts alone cannot
release that lock. Delete differs: known p2maps absence or explicit rejection
releases it; uploads cannot. Pre-publication validation failure releases its
unsent transaction. released_at is the generic release marker; upload_received
records geometry evidence in new writes. Migrate old overloaded timestamps
once without guessing their cause. Upgrade shared MQTT/HTTPS services together.

Probe scheduling
----------------
mqtt_probe starts a pass after a fresh accepted p2maps report and edit-topic
subscription. Missing maps are probed once per connection; later inventory
reports probe locked maps. Targets are deduplicated/sequential, with no timer
retries; a failure does not stop other targets. Pending deletion suppresses
probing. Real map ID + room 2147483647 + __nonexistent_room_probe__ is an
EXPERIMENTAL invalid edit, not read-only refresh or map switching. Rename
accepts signed-32-bit room values, unlike byte-sized merging. The user exercised
this probe, but loading a real map retains firmware map-loading failure risks.

Wire evidence (505 firmware p25-505+9.3.6+I3.8.149)
------------------------------------------------
Topic v011-irbthbu/things/<robot>/edit_req; correlate edit_resp with a fresh
positive signed-32-bit transaction_id. Envelope: p2map_id, transaction_id,
edit_cmd={command, params}; NOT the ordinary /cmd topic. Broker 0x4e1994 parses
the map ID's decimal suffix to native map_id. Missing hyphen gives zero;
malformed numbers can throw. Invented cloud IDs are not isolated safe targets.

Rename: rename_room(room_id string, room_name). Merge: arrange_room(room_ids
string list), using only verified adjacency; robot picks surviving ID/name.
Split: split_room(room_id, split_points flat four floats). Grid->world is
origin + grid*resolution, NO extra Y inversion/half-cell offset. Partner
parseSplitRoomReq 0xafb400 preserves float pairs; navigation
handleMapIDSetArrangerRoomInfoReq 0x12c6aa0 (loads at 0x12c7404) keeps
CRobotPose2D. processSplitScheme 0x143e694 -> generateSplitAllArea 0x1e0afa0 ->
getSplitDomainChain 0x1e0a130 -> initSplitLineInfor 0x1df47f0;
alignDoorPoseByContourPoint 0x1dc64e0/0x1dc6604 subtracts grid minima and divides
by resolution, establishing the inverse. The user split, renamed and remerged
a disposable map; unsuitable lines were rejected and refresh recovered. This
is physical evidence for this build/workflow, not every cut or firmware.

Delete: del_map with EMPTY params. Broker 0x624cdc selects service enum 0x2e;
translator 0x61f0ac injects numeric map_id (property 0x53; handler 0x4d4688)
from the envelope. Partner parseDeleteMapReq 0xafa580 expects that object,
sets ctrl_type=2 at 0xafa640 and removes map preferences. Do not inject invented
native IDs. Statically verified, not physically deletion-tested.
Creation/mapping-only missions remain unverified. Idle map switching is absent:
partner parseSetCurrentMapReq 0xafa3c0 has set_cur_map, but broker allowlist
0x624a44 does not. The user declined invalid-rename map-switch workarounds.

Retained research: rename_map is NOT a harmless upload request
------------------------------------------------------------
Broker envelope 0x5778ec -> translator 0x61eeb8/lookup 0x624a44 maps
rename_map(params.map_name) to service.rename_map. Partner parseRenameMapReq
0xafa758 serializes DeviceMapPlanParams (NOT DevicecChangeMapNameParams), plan
ID zero plus room-info entry. Request 0x7e0 -> local socket 0x3c ->
handleModifyMapNameReq 0x1367a10 -> state 0x1119 ->
handleMapIDSetNewMapNameReq 0x12c3300 (not metadata reset 0x12c2a80).
It assigns without comparing names. Current branch 0x12c2ee0 saves the plan
and reapplies virtual walls; noncurrent saves the list. Both call
appGetMapRsp(0x111a), sendHistoryMapToEntityApp 0x121bf60 and sendMapInfo
0x1225b00. Unknown nonzero ID fails at 0x12c3698; zero uses temporary-map
lookup 0x12c36b4, not a safe nonexistent ID.

sendAllMapData schedules handleGetAllMap 0x122aac0, reads scheme/grid/area,
sets RobotMap.maptype=3 at 0x122b270, then sendData 0x122d484 emits 0x116a.
Navigation 0x1366c34 -> wire 0x4f; partner 0x720004 -> 0x7e9.
CIrobotDataUpload -> doUploadHistoryMap 0xb2dd00 -> IrMapUploadInfo 0x11a2 ->
report 0xaba490/serializer 0xb1af48 emits map_upload(upload_id, map_type,
map_id, length). Broker decoder 0x5f82e4 -> process_map_upload_event 0x56ca44 ->
type-3 handle_smart_map_upload 0x56c6c8 -> event 0x193. Successful edits mark a
pending reason at 0x56b9c8, not a second map-data request. This establishes a
static upload route, NOT completed filexfer negotiation or readable map files.
Uploads must be enabled and partner/broker transport operational.

getSchemeInfo 0x122afe4 can fail through 0x122dce4/0x122dd48 to
loadMapFailedDeleteMap 0x1229f30, removing the map-list entry at 0x122a168.
Single-target history upload shares this worker. Same-name rename is therefore
NOT read-only; no general rename_map publisher is exposed. No such failure was
observed physically, but static tracing cannot prove map files healthy.

Other leads, not implemented cloud commands: request_smart_map_data 0x582b78 ->
translator 0x5f21c0 -> service.upload_by_mapid is internal. Cloud-connected
event 0x130 through 0x58215c starts replay 0x582f24; saved-map calls at
0x5831a4/0x5831b4 require a nonempty offline mission backlog. Reconnect alone
is insufficient. A short offline mission/reconnect is an unverified experiment,
not permission to auto-start cleaning. A ~24-second test yielded only a report;
a five-minute test yielded type-21 geometry/seven named rooms.
Ordinary command translator 0x614878/lookup 0x6169d0 has no established generic
upload request: its 19 entries cover cleaning/navigation/maintenance/reset;
unknown commands reject at 0x6148ec. RequestSmartMapData vtable GOT references
0x9af6d0 identify offline-replay construction/destruction, not a second cloud
constructor; this does not exclude all indirect paths.
Live-map handler 0x59f0c8 builds at 0x59f6b8, caller 0x59fefc
is mission-in-progress; serializer 0x620cf8 fixes map_type=0 for internal
service.upload_by_maptype, not a selectable docked saved map.
mapUploadAllowed parse 0x575ad4/dispatch 0x575c24 -> serializer 0x621778
inverts privacy map_uploads 0x6321d8. Partner 0xaab924 -> setter 0xabdfe8
stores the flag; separate uploader setter 0xaf0404 adjusts an enable timer.
No saved-map enumeration/refresh through this setting was established.

Research SHA-256 provenance (addresses are build-specific virtual addresses)
-------------------------------------------------------------------------
Connectivity broker:
3ed68cdbd9c94b0aa9da3ef2d526aaf724671da24118d1922653335273c8b33d
Matter chip-irobot-app:
a649509198fa587c038f6738a75ab89406193d573746b0a100a5408d5c5311f9
Partner systemApp:
932710fd8081941ce2828b3b56f8ff73da3ace658389e8f1b1c629a9b7a81793
Navigation everest-server:
e156a86a372313145630e3a7eab97a3e3f959b3b4588cb97798b87bab765a919
Keep proprietary firmware/private captures out of source. These notes record
offline analysis and labeled user trials, not authorization for live experiments.
"""

import contextlib
import json
import math
import re
from pathlib import Path
import sqlite3
import tarfile
import time
import unicodedata
import zlib
from os import PathLike
from typing import TypedDict
from typing import TYPE_CHECKING

from roomba_cloud.maps.customer_maps import geometry
from roomba_cloud.maps.catalog import map_catalog

if TYPE_CHECKING:
    from roomba_cloud.robot_commands import CommandSession, EditReceipt


class EditStatus(TypedDict):
    """Customer application result, independent of the MQTT delivery receipt."""

    status: str
    message: str


def normalize_room_name(name: object) -> str:
    """Trim to 1-64 Unicode code points; preserve international text and punctuation.

    Reject controls/formats/surrogates and line/paragraph separators BEFORE
    trimming, including a trailing newline. No grapheme counting or Unicode
    normalization. Mirror these categories/counting in web_ui.normalizeRoomName;
    existing robot names use room_catalog's independent decoder policy.
    """
    if not isinstance(name, str) or any(
        unicodedata.category(char) in ("Cc", "Cf", "Cs", "Zl", "Zp") for char in name
    ):
        raise ValueError("Room names cannot contain control or invisible formatting characters")
    name = name.strip()
    if not 1 <= len(name) <= 64:
        raise ValueError("Room names must contain 1-64 characters after trimming whitespace")
    return name


def initialize(database: str | PathLike[str]) -> None:
    """Migrate once, atomically, without resetting transaction IDs or active locks.

    Preserve AUTOINCREMENT correlation across restarts. Historical upload_received
    values are ambiguous; copy to released_at once, never resync on each startup.
    """
    with contextlib.closing(sqlite3.connect(database, timeout=10)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "CREATE TABLE IF NOT EXISTS room_edits ("
            "transaction_id INTEGER PRIMARY KEY AUTOINCREMENT, robot_id TEXT NOT NULL,"
            "created REAL NOT NULL, status TEXT NOT NULL, revision TEXT NOT NULL)"
        )
        columns = {row[1] for row in db.execute("PRAGMA table_info(room_edits)")}
        for name in ("map_id", "archive_token"):
            if name not in columns:
                db.execute(f"ALTER TABLE room_edits ADD COLUMN {name} TEXT")
        if "upload_received" not in columns:
            db.execute("ALTER TABLE room_edits ADD COLUMN upload_received REAL")
        if "operation" not in columns:
            db.execute("ALTER TABLE room_edits ADD COLUMN operation TEXT NOT NULL DEFAULT 'edit'")
        if "released_at" not in columns:
            db.execute("ALTER TABLE room_edits ADD COLUMN released_at REAL")
            # Older versions used upload_received for every kind of lock release.
            db.execute("UPDATE room_edits SET released_at=upload_received")


def connect_read(database: str | PathLike[str]) -> contextlib.closing[sqlite3.Connection]:
    return contextlib.closing(
        sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    )


def awaiting_upload(db: sqlite3.Connection, robot_id: str, map_id: str) -> bool:
    """Test any unreleased edit, including delete; status alone never removes a lock."""
    return (
        db.execute(
            "SELECT 1 FROM room_edits WHERE robot_id=? AND map_id=? "
            "AND released_at IS NULL LIMIT 1",
            (robot_id, map_id),
        ).fetchone()
        is not None
    )


def clear_uploaded_locks(
    shadow_db: str | PathLike[str],
    upload_db: str | PathLike[str],
    token: str,
    received_at: float,
) -> None:
    """Release pre-upload non-delete edits with exact robot/map geometry evidence.

    Called before pruning. Require created<=received_at so an older upload cannot
    release a later edit. Fresh identical geometry is sufficient: neither a changed
    digest nor selection of this version by the catalog is required.
    """
    from roomba_cloud.maps.preview import decode_geometry
    from roomba_cloud.maps.catalog import archive_maps
    from roomba_cloud.cloud_http import log

    initialize(shadow_db)
    with connect_read(upload_db) as uploads:
        uploads.execute("BEGIN")
        row = uploads.execute(
            "SELECT robot_id,bytes,sha256 FROM uploads WHERE token=? AND status='complete'",
            (token,),
        ).fetchone()
        if row is None:
            raise ValueError("Completed map upload is unavailable")
        path = Path(upload_db).parent / (token + ".tar.gz")
        maps = set()
        for identifier, _, compressed in archive_maps(path, row[1], row[2]):
            try:
                decode_geometry(compressed)
            except ValueError as exc:
                log(
                    "map_edit_unlock_skipped",
                    error_type=type(exc).__name__,
                    reason="uploaded map geometry is invalid",
                )
                continue
            maps.add(identifier)
        with contextlib.closing(sqlite3.connect(shadow_db, timeout=10)) as db, db:
            for identifier in maps:
                db.execute(
                    "UPDATE room_edits SET upload_received=?,released_at=? "
                    "WHERE robot_id=? AND map_id=? AND created<=? "
                    "AND released_at IS NULL AND operation!='delete'",
                    (received_at, received_at, row[0], identifier, received_at),
                )


def bootstrap_once(shadow_db, upload_db, robot_id, session, include_missing=True):
    """Probe missing maps once per connection and locked maps on inventory reports."""
    from roomba_cloud.cloud_http import log
    from roomba_cloud.robot_commands import CommandError
    from roomba_cloud.maps.catalog import saved_maps

    try:
        require_idle(shadow_db, robot_id)
        available = saved_maps(shadow_db, upload_db, robot_id) if include_missing else {}
        with connect_read(shadow_db) as db:
            row = db.execute(
                "SELECT document FROM shadows WHERE topic=?",
                (f"$aws/things/{robot_id}/shadow/name/ro-currentstate",),
            ).fetchone()
            locked = {
                item[0]
                for item in db.execute(
                    "SELECT DISTINCT map_id FROM room_edits WHERE robot_id=? "
                    "AND map_id IS NOT NULL AND released_at IS NULL AND operation!='delete'",
                    (robot_id,),
                )
            }
            deleting = {
                item[0]
                for item in db.execute(
                    "SELECT DISTINCT map_id FROM room_edits WHERE robot_id=? "
                    "AND operation='delete' AND released_at IS NULL",
                    (robot_id,),
                )
            }
        reported = json.loads(row[0]).get("state", {}).get("reported", {}) if row else {}
        inventory = reported.get("p2maps")
        if not isinstance(inventory, list):
            raise ValueError("Reported map inventory is unavailable")
        if not inventory:
            log("map_bootstrap_skipped", reason="robot reports no saved maps")
            return
        targets = []
        for item in inventory:
            identifier = item.get("p2map_id") if isinstance(item, dict) else None
            if isinstance(identifier, str) and identifier in deleting:
                continue
            if (
                isinstance(identifier, str)
                and identifier not in locked
                and (not include_missing or identifier in available)
            ):
                continue
            if (
                not isinstance(identifier, str)
                or not re.fullmatch(re.escape(robot_id) + r"-[1-9][0-9]{0,9}", identifier)
                or int(identifier.split("-")[1]) > 2147483647
            ):
                raise ValueError("Cannot verify reported partner map IDs")
            if identifier not in targets:
                targets.append(identifier)
        if not targets:
            log("map_bootstrap_skipped", reason="all reported maps available")
        for target in targets:
            try:
                with contextlib.closing(sqlite3.connect(shadow_db, timeout=10)) as db, db:
                    cursor = db.execute(
                        "INSERT INTO room_edits(robot_id,created,status,revision) "
                        "VALUES (?,?,'pending','bootstrap')",
                        (robot_id, time.time()),
                    )
                    transaction_id = cursor.lastrowid

                def document(target=target, transaction_id=transaction_id):
                    require_idle(shadow_db, robot_id)
                    return {
                        "p2map_id": target,
                        "transaction_id": transaction_id,
                        "edit_cmd": {
                            "command": "rename_room",
                            "params": {
                                "room_id": "2147483647",
                                "room_name": "__nonexistent_room_probe__",
                            },
                        },
                    }

                session.publish_once(session.edit_topic, document)
                log(
                    "map_bootstrap_sent",
                    robot_id=robot_id,
                    map_id=target,
                    transaction_id=transaction_id,
                    execution="unconfirmed",
                    retries=False,
                )
            except (CommandError, OSError, sqlite3.Error, ValueError) as exc:
                log(
                    "map_bootstrap_failed",
                    map_id=target,
                    error_type=type(exc).__name__,
                    retries=False,
                )
    except (
        CommandError,
        OSError,
        sqlite3.Error,
        ValueError,
        TypeError,
        KeyError,
        tarfile.TarError,
        zlib.error,
    ) as exc:
        log("map_bootstrap_failed", error_type=type(exc).__name__, retries=False)
    finally:
        session.map_refresh_in_progress = False


def edit_status(database: str | PathLike[str], robot_id: str, transaction_id: int) -> EditStatus:
    """Reconcile deletions with known inventory; unknown inventory is not deletion.

    After 60 seconds pending becomes unconfirmed for display only: no unlock or
    retry follows. A successful ordinary edit response still needs fresh geometry.
    """
    with connect_read(database) as db:
        row = db.execute(
            "SELECT status,created,operation,map_id FROM room_edits "
            "WHERE robot_id=? AND transaction_id=?",
            (robot_id, transaction_id),
        ).fetchone()
    if row is None:
        raise ValueError("Unknown room edit transaction")
    if row[2] == "delete":
        from roomba_cloud.maps.catalog import reported_maps

        maps = reported_maps(database, robot_id)
        if maps is not None and row[3] not in maps:
            with contextlib.closing(sqlite3.connect(database, timeout=10)) as db, db:
                db.execute(
                    "UPDATE room_edits SET status='confirmed',released_at=? "
                    "WHERE robot_id=? AND transaction_id=? AND released_at IS NULL",
                    (time.time(), robot_id, transaction_id),
                )
            return {
                "status": "deleted",
                "message": "Robot inventory confirms the map was deleted. Reload the map list.",
            }
        if row[0] != "rejected":
            return {
                "status": "pending" if time.time() - row[1] < 60 else "unconfirmed",
                "message": "Waiting for the robot inventory to confirm deletion. Reload to check; do not retry automatically.",
            }
    status = row[0]
    if status == "pending" and time.time() - row[1] >= 60:
        status = "unconfirmed"
    messages = {
        "pending": "Sent; waiting for the robot's edit result.",
        "confirmed": "Robot accepted the edit. Reload the map to check the updated rooms.",
        "rejected": "Robot rejected the edit. Reload the map before trying again.",
        "unconfirmed": "Edit result is unknown. Do not assume failure or retry; reload and check the robot map.",
        "responded": "Robot replied. Reload the map to verify the result; response status is not yet interpreted.",
    }
    return {"status": status, "message": messages[status]}


def require_idle(database: str | PathLike[str], robot_id: str) -> None:
    from roomba_cloud.maps.controls import require_no_mission

    require_no_mission(database, robot_id)


def require_room_crossing(points, contour):
    """Require one transverse cut, rejecting tangency or repeated concave-room entry.

    Vertex intersections are deduplicated with a numeric tolerance. This screen
    cannot guarantee acceptance: firmware validates the cut and chooses room IDs.
    """
    start, end = points
    dx, dy = end[0] - start[0], end[1] - start[1]
    sides = [dx * (y - start[1]) - dy * (x - start[0]) for x, y in contour]
    intersections: list[float] = []
    for index, (x, y) in enumerate(contour):
        other_x, other_y = contour[(index + 1) % len(contour)]
        ex, ey = other_x - x, other_y - y
        denominator = dx * ey - dy * ex
        if abs(denominator) < 1e-9:
            continue
        px, py = x - start[0], y - start[1]
        t = (px * ey - py * ex) / denominator
        u = (px * dy - py * dx) / denominator
        if -1e-9 <= t <= 1 + 1e-9 and -1e-9 <= u <= 1 + 1e-9:
            if all(abs(t - previous) > 1e-9 for previous in intersections):
                intersections.append(t)
                if len(intersections) > 2:
                    break
    if not min(sides) < -1e-9 or not max(sides) > 1e-9 or len(intersections) != 2:
        raise ValueError(
            "Draw a single cut across the selected room, with endpoints on or beyond its walls"
        )


def validate_edit(shadow_db, upload_db, robot_id, edit):
    """Check mission, current revision, exact fields and source geometry before reservation.

    Merge eligibility comes from uploaded adjacency, never visual proximity.
    Return the unmodified model for split conversion, not a smoothed display mask.
    """
    if not isinstance(edit, dict):
        raise ValueError("Room edit must be an object")
    operation = edit.get("operation")
    extras = {"rename": {"name"}, "split": {"points"}, "merge": {"other_room_id"}, "delete": set()}
    if not isinstance(operation, str) or operation not in extras:
        raise ValueError("Unsupported room edit operation")
    identity = {"map_id", "version", "revision", "operation"}
    if operation != "delete":
        identity.add("room_id")
    if set(edit) != identity | extras[operation]:
        raise ValueError("Invalid room edit fields")
    if any(not isinstance(edit[key], str) for key in identity):
        raise ValueError("Invalid map or room identity")
    require_idle(shadow_db, robot_id)
    with map_catalog(shadow_db, upload_db, robot_id) as maps:
        item = maps.get(edit["map_id"])
        if (
            item is None
            or item["version"] != edit["version"]
            or item["archive"][3] != edit["revision"]
        ):
            raise ValueError("Map changed or is unavailable; reload before editing")
        model = geometry(*item["archive"], edit["map_id"], item["archive_version"])
        with connect_read(shadow_db) as db:
            if awaiting_upload(db, robot_id, edit["map_id"]):
                raise ValueError("Waiting for a new map upload after the previous edit")
    if operation == "delete":
        return model
    if edit["room_id"] not in model.rooms:
        raise ValueError("Unknown room; reload before editing")
    if operation == "rename":
        normalize_room_name(edit["name"])
    elif operation == "merge":
        other = edit["other_room_id"]
        if not isinstance(other, str) or other not in model.rooms or other == edit["room_id"]:
            raise ValueError("Select two different known rooms to merge")
        if model.adjacent_rooms is None:
            raise ValueError("Room adjacency is unavailable in this map; merging is disabled")
        if other not in model.adjacent_rooms[edit["room_id"]]:
            raise ValueError("Selected rooms are not adjacent in the robot's map")
    else:
        points = edit["points"]
        if (
            not isinstance(points, list)
            or len(points) != 2
            or any(
                not isinstance(point, list)
                or len(point) != 2
                or any(
                    type(value) not in (int, float) or not math.isfinite(value) for value in point
                )
                for point in points
            )
        ):
            raise ValueError("Choose two finite split-line endpoints")
        if any(not 0 <= x < model.width or not 0 <= y < model.height for x, y in points):
            raise ValueError("Split-line endpoints must be inside the map")
        if math.dist(*points) < 1:
            raise ValueError("Split-line endpoints must be distinct")
        require_room_crossing(points, model.contours[edit["room_id"]])
    return model


def wire_command(edit, model):
    """Translate supported cloud edits with safe decimal map/byte-sized room IDs.

    Bootstrap's deliberately invalid signed-32-bit room bypasses this user-edit
    translator. Split coordinates use world units, not preview pixel centers.
    """
    if not re.fullmatch(r"[A-Fa-f0-9]{32}-[1-9][0-9]{0,9}", edit["map_id"]):
        raise ValueError("Map ID cannot be safely translated to a partner map ID")
    if int(edit["map_id"].split("-")[1]) > 2147483647:
        raise ValueError("Map ID exceeds the partner integer range")
    operation = edit["operation"]
    if operation == "delete":
        return {"command": "del_map", "params": {}}
    room = edit["room_id"]
    if not re.fullmatch(r"[0-9]{1,3}", room) or not 1 <= int(room) <= 255:
        raise ValueError("Room ID is outside the verified editing range")
    if operation == "rename":
        return {
            "command": "rename_room",
            "params": {"room_id": room, "room_name": normalize_room_name(edit["name"])},
        }
    if operation == "merge":
        other = edit["other_room_id"]
        if not re.fullmatch(r"[0-9]{1,3}", other) or not 1 <= int(other) <= 255:
            raise ValueError("Room ID is outside the verified editing range")
        return {"command": "arrange_room", "params": {"room_ids": [room, other]}}
    if operation == "split":
        return {
            "command": "split_room",
            "params": {
                "room_id": room,
                "split_points": [
                    model.origin[axis] + point[axis] * model.resolution
                    for point in edit["points"]
                    for axis in (0, 1)
                ],
            },
        }
    raise ValueError("Unsupported room edit operation")


def receive_result(database: str | PathLike[str], robot_id: str, payload: bytes) -> None:
    """Correlate bounded edit_resp; ordinary rejection still waits for geometry.

    Only pending/unconfirmed rows change status. Explicit deletion rejection
    releases once; repeated messages cannot rewrite the release timestamp.
    """
    if len(payload) > 8192:
        raise ValueError("Room edit response exceeds size limit")
    document = json.loads(payload)
    if (
        not isinstance(document, dict)
        or type(document.get("transaction_id")) is not int
        or not isinstance(document.get("status"), str)
        or not 1 <= len(document["status"]) <= 64
    ):
        raise ValueError("Invalid room edit response")
    status = (
        "rejected"
        if document["status"] == "error" or "error" in document
        else "confirmed"
        if document["status"] == "success"
        else "responded"
    )
    with contextlib.closing(sqlite3.connect(database, timeout=10)) as db, db:
        db.execute(
            "UPDATE room_edits SET status=? "
            "WHERE robot_id=? AND transaction_id=? AND status IN ('pending','unconfirmed')",
            (status, robot_id, document["transaction_id"]),
        )
        if status == "rejected":
            db.execute(
                "UPDATE room_edits SET released_at=? WHERE robot_id=? "
                "AND transaction_id=? AND operation='delete' AND status='rejected' "
                "AND released_at IS NULL",
                (time.time(), robot_id, document["transaction_id"]),
            )


def submit(
    shadow_db: str | PathLike[str],
    upload_db: str | PathLike[str],
    robot_id: str,
    session: "CommandSession",
    edit: object,
) -> "EditReceipt":
    """Validate, reserve a durable map lock, then publish exactly once.

    The final mission check may reject an unsent edit and release its transaction.
    CommandError can follow a write, so retain an unconfirmed lock rather than
    infer nonexecution. Never replay on reconnect or automatically retry.
    """
    from roomba_cloud.robot_commands import CommandError

    model = validate_edit(shadow_db, upload_db, robot_id, edit)
    if not isinstance(edit, dict):
        raise ValueError("Room edit must be an object")
    command = wire_command(edit, model)
    with map_catalog(shadow_db, upload_db, robot_id) as maps:
        item = maps.get(edit["map_id"])
        if item is None or item["archive"][3] != edit["revision"]:
            raise ValueError("Map changed; reload before editing")
        archive_token = Path(item["archive"][0]).name
    now = time.time()
    with contextlib.closing(sqlite3.connect(shadow_db, timeout=10)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        if awaiting_upload(db, robot_id, edit["map_id"]):
            raise ValueError("Waiting for a new map upload after the previous edit")
        if db.execute(
            "SELECT 1 FROM room_edits WHERE robot_id=? AND status='pending' "
            "AND released_at IS NULL AND revision!='bootstrap' "
            "AND created>?",
            (robot_id, now - 60),
        ).fetchone():
            raise ValueError("Another room edit is awaiting a robot result")
        cursor = db.execute(
            "INSERT INTO room_edits(robot_id,created,status,revision,map_id,archive_token,operation) "
            "VALUES (?,?,'pending',?,?,?,?)",
            (robot_id, now, edit["revision"], edit["map_id"], archive_token, edit["operation"]),
        )
        identifier = cursor.lastrowid
        if identifier is None:
            raise ValueError("Room edit transaction was not created")
        if identifier > 2147483647:
            raise ValueError("Room edit transaction ID limit reached")

    def document():
        require_idle(shadow_db, robot_id)
        return {"p2map_id": edit["map_id"], "transaction_id": identifier, "edit_cmd": command}

    try:
        session.publish_once(session.edit_topic, document)
    except ValueError:
        with contextlib.closing(sqlite3.connect(shadow_db, timeout=10)) as db, db:
            db.execute(
                "UPDATE room_edits SET status='rejected',released_at=? WHERE transaction_id=?",
                (time.time(), identifier),
            )
        raise
    except CommandError as exc:
        with contextlib.closing(sqlite3.connect(shadow_db, timeout=10)) as db, db:
            db.execute(
                "UPDATE room_edits SET status='unconfirmed' "
                "WHERE transaction_id=? AND status='pending'",
                (identifier,),
            )
        raise CommandError(
            f"Edit {identifier}: delivery or execution is uncertain; reload the map before retrying"
        ) from exc
    return {"transaction_id": identifier}


def editor_maps(shadow_db, upload_db, robot_id):
    """Return source coordinates/capabilities while holding the catalog read lock.

    Lock/deletion state is separate from operations so UI can disable controls
    while retaining inspection and Reload. No renderer smoothing enters this API.
    """
    from roomba_cloud.maps.controls import map_control_state

    control = map_control_state(shadow_db, robot_id)
    with map_catalog(shadow_db, upload_db, robot_id) as maps:
        result = []
        for map_id, item in sorted(maps.items()):
            model = geometry(*item["archive"], map_id, item["archive_version"])
            with connect_read(shadow_db) as db:
                locked = awaiting_upload(db, robot_id, map_id)
                deleting = (
                    db.execute(
                        "SELECT 1 FROM room_edits WHERE robot_id=? AND map_id=? "
                        "AND operation='delete' AND released_at IS NULL",
                        (robot_id, map_id),
                    ).fetchone()
                    is not None
                )
            result.append(
                {
                    "id": map_id,
                    "version": item["version"],
                    "revision": item["archive"][3],
                    "width": model.width,
                    "height": model.height,
                    "origin": model.origin,
                    "resolution": model.resolution,
                    "rooms": [
                        {
                            "id": key,
                            "name": model.rooms[key],
                            "contour": points,
                            "adjacent_room_ids": (
                                list(model.adjacent_rooms[key])
                                if model.adjacent_rooms is not None
                                else []
                            ),
                        }
                        for key, points in model.contours.items()
                    ],
                    "walls": model.structural_lines,
                    "operations": (
                        ["rename", "merge", "split", "delete"]
                        if model.adjacent_rooms is not None
                        else ["rename", "split", "delete"]
                    ),
                    "awaiting_upload": locked,
                    "pending_deletion": deleting,
                }
            )
        return {"maps": result, "map_controls": control}
