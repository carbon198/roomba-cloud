"""Resolve authoritative robot inventory against bounded SCMap room archives.

Inventory and geometry have different authority: reported p2maps controls which
IDs/versions exist, while completed archives supply rooms and geometry. Never
resurrect a deleted map from retained bytes or treat missing bytes as deletion.
None inventory means unknown, whereas an empty list means known empty.

Accept type-3 saved maps and type-21 mission snapshots from complete smart/mission
transfers. Reports without map members create no maps. Newest usable data wins
per map: smart-map versions must match inventory; mission snapshot versions may
differ. Keep archive_version for decoding that member, but use the robot's saved
version in segment IDs and cleaning commands. This does not rewrite p2maps.
Mission-derived targeting still needs physical validation on the robot.

Every archive is robot-scoped, digest checked, member/size bounded and read
without extraction. Read transactions protect file users from pruning; callers
that need geometry must keep map_catalog open until their file reads finish.
Existing archives are discovered after restart without a new cleaning run.
"""

import contextlib
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import tarfile
import zlib

IDENTIFIER = re.compile(r"[A-Za-z0-9_.-]{1,128}")
MAX_MAP_BYTES = 8 * 1024 * 1024


def reported_maps(shadow_db, robot_id):
    """Return ID->version inventory, None if unknown, or {} if known empty.

    Preserve historical last-value-wins handling of duplicate IDs. Malformed
    inventory is an explicit error, not empty data that could delete HA entities.
    """
    with contextlib.closing(
        sqlite3.connect(Path(shadow_db).resolve().as_uri() + "?mode=ro", uri=True)
    ) as db:
        row = db.execute(
            "SELECT document FROM shadows WHERE topic=?",
            (f"$aws/things/{robot_id}/shadow/name/ro-currentstate",),
        ).fetchone()
    maps = json.loads(row[0]).get("state", {}).get("reported", {}).get("p2maps") if row else None
    if maps is None:
        return None
    if not isinstance(maps, list) or len(maps) > 10:
        raise ValueError("Invalid reported map list")
    current = {}
    for item in maps:
        if not isinstance(item, dict) or any(
            not isinstance(item.get(k), str) or not IDENTIFIER.fullmatch(item[k])
            for k in ("p2map_id", "p2mapv_id")
        ):
            raise ValueError("Invalid reported map identity")
        current[item["p2map_id"]] = item["p2mapv_id"]
    return current


def varint(data, offset):
    value = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            raise ValueError("Truncated protobuf varint")
        byte = data[offset]
        offset += 1
        if shift == 63 and byte > 1:
            raise ValueError("Oversized protobuf varint")
        value |= (byte & 127) << shift
        if byte < 128:
            return value, offset
    raise ValueError("Oversized protobuf varint")


def wire_fields(data):
    """Iterate bounded protobuf primitives; unknown field numbers remain skippable."""
    offset = 0
    while offset < len(data):
        tag, offset = varint(data, offset)
        number, wire = tag >> 3, tag & 7
        if not 1 <= number < 2**29:
            raise ValueError("Invalid protobuf field number")
        if wire == 0:
            value, offset = varint(data, offset)
        else:
            if wire == 2:
                size, offset = varint(data, offset)
            elif wire in (1, 5):
                size = 8 if wire == 1 else 4
            else:
                raise ValueError("Unsupported protobuf wire type")
            if offset + size > len(data):
                raise ValueError("Truncated protobuf field")
            value = data[offset : offset + size]
            offset += size
        yield number, wire, value


def decompress_map(compressed):
    """Bound zlib expansion and reject truncation/trailing compressed streams."""
    decoder = zlib.decompressobj()
    data = decoder.decompress(compressed, MAX_MAP_BYTES + 1)
    if (
        len(data) > MAX_MAP_BYTES
        or not decoder.eof
        or decoder.unused_data
        or decoder.unconsumed_tail
    ):
        raise ValueError("Invalid or oversized compressed map")
    return data


def decode_rooms(compressed):
    """Read RobotMap field 12's room IDs/names; never invent a room table.

    Robot-reported names retain the decoder's historical 128-character acceptance;
    the stricter 64-code-point rule in room_edits applies only to new user input.
    Empty firmware names get a neutral Segment ID label.
    """
    data = decompress_map(compressed)
    rooms = {}
    for number, wire, value in wire_fields(data):
        if number != 12:
            continue
        if wire != 2 or len(rooms) >= 100:
            raise ValueError("Invalid room table")
        fields = {}
        for key, kind, item in wire_fields(value):
            if key in (1, 2):
                if key in fields or kind != (0 if key == 1 else 2):
                    raise ValueError("Invalid room record")
                fields[key] = item
        identifier = fields.get(1)
        if type(identifier) is not int or not 0 < identifier < 2**32:
            raise ValueError("Missing or invalid room ID")
        name = fields.get(2, b"").decode("utf-8")
        if len(name) > 128 or any(ord(char) < 32 for char in name):
            raise ValueError("Invalid room name")
        if str(identifier) in rooms:
            raise ValueError("Duplicate room ID")
        rooms[str(identifier)] = name or f"Segment {identifier}"
    if not rooms:
        raise ValueError("Map has no verified room table")
    return rooms


@lru_cache(maxsize=16)
def read_archive(path, size, modified, digest, smart_only=False):
    """Cache only small room-table metadata; size/mtime/digest invalidate file revisions."""
    return [
        (map_id, version, decode_rooms(compressed))
        for map_id, version, compressed in archive_maps(path, size, digest, smart_only)
    ]


def archive_maps(path, size, digest, smart_only=False):
    """Read verified saved-map members without extracting or caching binary payloads."""
    if size > 64 * 1024 * 1024:
        raise ValueError("Map archive exceeds decoding limit")
    # Only the current small metadata/table result is cached, never raw map geometry.
    with Path(path).open("rb") as source:
        actual = hashlib.file_digest(source, "sha256").hexdigest()
        if actual != digest:
            raise ValueError("Map archive digest mismatch")
        source.seek(0)
        with tarfile.open(fileobj=source, mode="r:*") as archive:
            members = []
            for member in archive:
                members.append(member)
                if len(members) > 20 or member.offset_data + member.size > 64 * 1024 * 1024:
                    raise ValueError("Map archive exceeds expanded size/member limit")
            manifests = [m for m in members if m.name == "manifest.json"]
            if len(manifests) != 1 or not manifests[0].isfile() or manifests[0].size > 65536:
                raise ValueError("Invalid map manifest member")
            manifest = json.load(archive.extractfile(manifests[0]))
            if (
                not isinstance(manifest, dict)
                or type(manifest.get("schema_version")) is not int
                or manifest["schema_version"] != 1
                or not isinstance(manifest.get("files"), list)
            ):
                raise ValueError("Unsupported map manifest")
            for entry in manifest["files"]:
                if not isinstance(entry, dict):
                    raise ValueError("Invalid manifest entry")
                metadata = entry.get("p2mapv")
                if entry.get("type") != "p2mapv":
                    if smart_only:
                        raise ValueError("Archive contains non-map data")
                    continue
                if not isinstance(metadata, dict):
                    raise ValueError("Invalid map identity")
                # Type 21 mission snapshots use the same verified map schema.
                if metadata.get("p2map_type") not in ((3,) if smart_only else (3, 21)):
                    if smart_only:
                        raise ValueError("Archive contains an unverified map type")
                    continue
                if any(
                    not isinstance(metadata.get(k), str) or not IDENTIFIER.fullmatch(metadata[k])
                    for k in ("p2map_id", "p2mapv_id")
                ):
                    raise ValueError("Invalid map identity")
                matching = [m for m in members if m.name == entry.get("name")]
                if (
                    len(matching) != 1
                    or not matching[0].isfile()
                    or matching[0].size > MAX_MAP_BYTES
                ):
                    raise ValueError("Invalid map binary member")
                compressed = archive.extractfile(matching[0]).read(MAX_MAP_BYTES + 1)
                yield metadata["p2map_id"], metadata["p2mapv_id"], compressed


@contextlib.contextmanager
def map_catalog(shadow_db, upload_db, robot_id):
    """Resolve current versions while holding the archive-pruning read lock."""
    if upload_db is None:
        yield {}
        return
    current = reported_maps(shadow_db, robot_id)
    if not current:
        yield {}
        return
    with contextlib.closing(
        sqlite3.connect(Path(upload_db).resolve().as_uri() + "?mode=ro", uri=True)
    ) as db:
        # Hold the read transaction through archive reads. Pruning must commit its
        # tombstones before unlinking, so an in-flight catalog cannot lose a file.
        db.execute("BEGIN")
        rows = db.execute(
            "SELECT token,bytes,sha256,xfer_type FROM uploads WHERE robot_id=? "
            "AND xfer_type IN ('uploadP2MapSmart','uploadP2MapMission') "
            "AND status='complete' ORDER BY rowid DESC LIMIT 1000",
            (robot_id,),
        ).fetchall()
        yield catalog_maps(rows, upload_db, current)


def saved_maps(shadow_db, upload_db, robot_id):
    """Return detached metadata; callers reading archive files must use map_catalog instead."""
    with map_catalog(shadow_db, upload_db, robot_id) as maps:
        return maps


def catalog_maps(rows, upload_db, current):
    """Select newest acceptable members while preserving saved versus archive versions."""
    found = {}
    for token, size, digest, transfer_type in rows:
        if not re.fullmatch(r"[a-f0-9]{64}", token):
            raise ValueError("Invalid stored upload token")
        path = Path(upload_db).parent / (token + ".tar.gz")
        info = path.stat()
        if info.st_size != size or path.is_symlink():
            raise ValueError("Invalid stored map archive")
        for map_id, version, rooms in read_archive(str(path), size, info.st_mtime_ns, digest):
            if (
                map_id not in current
                or map_id in found
                or (transfer_type == "uploadP2MapSmart" and current[map_id] != version)
            ):
                continue
            found[map_id] = {
                "version": current[map_id],
                "archive_version": version,
                "rooms": rooms,
                "archive": (str(path), size, info.st_mtime_ns, digest),
            }
        if set(found) == set(current):
            break
    return found


def map_segments(maps):
    """Make versioned room identities so stale UI selections cannot target changed maps."""
    segments = [
        {
            "id": f"{map_id}:{item['version']}:{region_id}",
            "name": name,
            "group": f"Map {map_id}",
            "map_id": map_id,
            "map_version": item["version"],
            "region_id": region_id,
        }
        for map_id, item in sorted(maps.items())
        for region_id, name in item["rooms"].items()
    ]
    if len(segments) > 100:
        raise ValueError("Room catalog exceeds supported segment count")
    return segments


def catalog(shadow_db, upload_db, robot_id):
    return map_segments(saved_maps(shadow_db, upload_db, robot_id))


def room_command(shadow_db, upload_db, robot_id, segment_ids):
    """Translate distinct current rooms from one map to the verified start payload.

    A separate active-map selector is unnecessary for room-targeted cleaning:
    p2map_id/user_p2mapv_id identify the map. Do not combine rooms across maps or
    accept stale versioned segment IDs. This does not implement idle map switching.
    """
    if (
        not isinstance(segment_ids, list)
        or not 1 <= len(segment_ids) <= 100
        or any(not isinstance(item, str) for item in segment_ids)
        or len(set(segment_ids)) != len(segment_ids)
    ):
        raise ValueError("Select one or more distinct known segments")
    known = {segment["id"]: segment for segment in catalog(shadow_db, upload_db, robot_id)}
    if any(identifier not in known for identifier in segment_ids):
        raise ValueError("Selected segments are unknown or their map version is unavailable")
    selected = [known[identifier] for identifier in segment_ids]
    if len({segment["map_id"] for segment in selected}) != 1:
        raise ValueError("Rooms from different maps cannot be cleaned together")
    return {
        "p2map_id": selected[0]["map_id"],
        "user_p2mapv_id": selected[0]["map_version"],
        "select_all": False,
        "regions": [{"region_id": segment["region_id"], "type": "rid"} for segment in selected],
    }
