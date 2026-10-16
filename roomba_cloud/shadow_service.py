"""Persist robot-scoped AWS-style reported shadows, not a desired-state broker.

The MQTT client ID scopes every shadow lookup; cross-robot topics must never
mutate another robot. Recursive patches delete nulls and replace arrays/scalars,
with per-leaf timestamps and monotonic versions. Non-finite JSON, excessive
nesting/size, invalid client tokens and version conflicts receive rejections.
The 100-shadow limit is deployment-wide, not per robot.

Settings are one-shot deltas: reserve a version but never persist a desired queue
to replay on reconnect. The 505 reports applied settings while clearing desired
fields to null, so deletion-only desired patches are accepted; non-null desired
values remain unsupported. MQTT delivery does not confirm setting adoption.

MQTT leases distinguish online state from durable connection/registration history.
Pings renew leases but only publications count as data received. Leases expire
after crashes; last IP is the observed peer (possibly NAT/proxy), not identity.
Maps/trails/shadows can expose private home data: directories and DBs are private,
and wakeups carry only robot IDs after changes, never shadow contents.
"""

import copy
import contextlib
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import time
import uuid


def reject_constant(value):
    raise ValueError("Non-finite JSON number")


def finite_float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Non-finite JSON number")
    return result


def merge_reported(target, timestamps, patch, now):
    """Apply AWS-style null deletion/object merge while keeping metadata aligned."""
    for key, value in patch.items():
        if value is None:
            target.pop(key, None)
            timestamps.pop(key, None)
        elif isinstance(value, dict):
            if not isinstance(target.get(key), dict):
                target[key] = {}
                timestamps[key] = {}
            merge_reported(target[key], timestamps[key], value, now)
        else:
            target[key] = copy.deepcopy(value)
            timestamps[key] = {"timestamp": now}


def clears_desired(value):
    """Allow the firmware's acknowledgement cleanup without accepting desired writes."""
    return value is None or (
        isinstance(value, dict) and all(clears_desired(item) for item in value.values())
    )


class ShadowService:
    """Own private persistent shadows/history; open a short-lived DB connection per operation."""

    def __init__(self, path):
        from roomba_cloud.telemetry_events import TelemetryNotifier

        self._notifier = TelemetryNotifier(path)
        self.path = Path(path)
        if self.path.parent.stat().st_mode & 0o077:
            raise ValueError("Shadow database parent must be private (chmod 700)")
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            info = self.path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise ValueError(
                    "Shadow database must be a regular private mode-0600 file"
                ) from None
        else:
            os.close(fd)
        with contextlib.closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS shadows "
                "(topic TEXT PRIMARY KEY, document TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS mqtt_sessions "
                "(session TEXT PRIMARY KEY, robot_id TEXT NOT NULL, expires REAL NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS robot_data "
                "(robot_id TEXT PRIMARY KEY, received_at INTEGER NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS live_positions "
                "(robot_id TEXT PRIMARY KEY, document TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS robot_connections "
                "(robot_id TEXT PRIMARY KEY, ip TEXT NOT NULL, "
                "first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL, sku TEXT)"
            )

    @staticmethod
    def record_connection(db, robot_id, ip, sku=None):
        now = int(time.time())
        db.execute(
            "INSERT INTO robot_connections VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(robot_id) DO UPDATE SET ip=excluded.ip, last_seen=excluded.last_seen, "
            "sku=COALESCE(excluded.sku, robot_connections.sku)",
            (robot_id, ip, now, now, sku),
        )

    def record_registration(self, robot_id, ip, sku=None):
        with contextlib.closing(sqlite3.connect(self.path)) as db, db:
            self.record_connection(db, robot_id, ip, sku)

    def start_session(self, robot_id, lease, peer_ip=None):
        """Create a unique lease so an old disconnect cannot remove a newer session."""
        session = uuid.uuid4().hex
        with contextlib.closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DELETE FROM mqtt_sessions WHERE expires < ?", (time.time(),))
            db.execute(
                "INSERT INTO mqtt_sessions VALUES (?, ?, ?)",
                (session, robot_id, time.time() + lease),
            )
            if peer_ip is not None:
                self.record_connection(db, robot_id, peer_ip)
        self.notify(robot_id)
        return session

    def notify(self, robot_id):
        self._notifier.notify(robot_id)

    def setting_delta(self, robot_id, key, value):
        """Reserve a monotonic delta version without persisting desired state for replay."""
        from roomba_cloud.robot_settings import setting_patch

        patch = setting_patch(self.path, robot_id, key, value)
        topic = f"$aws/things/{robot_id}/shadow/name/rw-settings"
        with contextlib.closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT document FROM shadows WHERE topic=?", (topic,)).fetchone()
            if row is None:
                raise ValueError("Robot settings shadow is unavailable")
            document = json.loads(row[0])
            document["version"] += 1
            db.execute("UPDATE shadows SET document=? WHERE topic=?", (json.dumps(document), topic))
        return {"version": document["version"], "timestamp": int(time.time()), "state": patch}

    def touch_session(self, session, lease, data_received=False):
        now = time.time()
        with contextlib.closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE mqtt_sessions SET expires=? WHERE session=?", (now + lease, session))
            db.execute(
                "UPDATE robot_connections SET last_seen=? WHERE robot_id IN "
                "(SELECT robot_id FROM mqtt_sessions WHERE session=?)",
                (int(now), session),
            )
            if data_received:
                db.execute(
                    "INSERT OR REPLACE INTO robot_data "
                    "SELECT robot_id, ? FROM mqtt_sessions WHERE session=?",
                    (int(now), session),
                )

    def end_session(self, session):
        """Remove only this connection's lease and wake customer readers after commit."""
        with contextlib.closing(sqlite3.connect(self.path)) as db, db:
            row = db.execute(
                "SELECT robot_id FROM mqtt_sessions WHERE session=?", (session,)
            ).fetchone()
            db.execute("DELETE FROM mqtt_sessions WHERE session=?", (session,))
        if row:
            self.notify(row[0])

    def respond(self, topic, payload, client_id):
        """Handle get/update or route custom live telemetry; unrelated topics yield no reply.

        Responses echo a validated clientToken for correlation. BEGIN IMMEDIATE
        serializes version read/modify/write. Mission identity resets live trails
        in the same transaction as the current-state report, avoiding stale
        overlays being attached to a newly reported mission.
        """
        prefix = f"$aws/things/{client_id}/shadow/"
        if not topic.startswith(prefix):
            from roomba_cloud.maps.live_position import ingest

            ingest(self.path, client_id, topic, payload)
            return None
        suffix = topic[len(prefix) :].split("/")
        if len(suffix) == 1:
            action = suffix[0]
        elif len(suffix) == 3 and suffix[0] == "name" and suffix[1]:
            action = suffix[2]
        else:
            return None
        if action not in ("get", "update"):
            return None
        base = topic.rsplit("/", 1)[0]
        now = int(time.time())
        token = None

        def rejection(code, message):
            result = {"code": code, "message": message, "timestamp": now}
            if token is not None:
                result["clientToken"] = token
            return topic + "/rejected", result

        if len(payload) > 256 * 1024:
            return rejection(413, "Request exceeds diagnostic shadow limit")
        try:
            request = json.loads(payload, parse_constant=reject_constant, parse_float=finite_float)
        except (ValueError, UnicodeDecodeError, RecursionError):
            return rejection(400, "Invalid JSON request")
        if not isinstance(request, dict):
            return rejection(400, "Request must be an object")
        token = request.get("clientToken")
        if token is not None and (
            not isinstance(token, str)
            or len(token) > 64
            or any(0xD800 <= ord(c) <= 0xDFFF for c in token)
        ):
            token = None
            return rejection(400, "Invalid clientToken")
        with contextlib.closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT document FROM shadows WHERE topic=?", (base,)).fetchone()
            document = json.loads(row[0]) if row else None
            if action == "get":
                if document is None:
                    return rejection(404, "No shadow exists with name")
                result = copy.deepcopy(document)
                result["timestamp"] = now
            else:
                state = request.get("state")
                if (
                    not isinstance(state, dict)
                    or "reported" not in state
                    or set(state) - {"reported", "desired"}
                    or not isinstance(state["reported"], dict)
                ):
                    return rejection(
                        400, "Reported-state object required; only desired deletions are supported"
                    )
                try:
                    if "desired" in state and not clears_desired(state["desired"]):
                        return rejection(400, "Only desired deletions are supported")
                except RecursionError:
                    return rejection(400, "State nesting exceeds diagnostic limit")
                version = request.get("version")
                if version is not None:
                    if isinstance(version, bool) or not isinstance(version, int):
                        return rejection(400, "Invalid version")
                    if version != (document["version"] if document else 0):
                        return rejection(409, "Version conflict")
                if document is None:
                    count = db.execute("SELECT count(*) FROM shadows").fetchone()[0]
                    if count >= 100:
                        return rejection(413, "Diagnostic shadow count limit reached")
                    document = {
                        "state": {"reported": {}},
                        "metadata": {"reported": {}},
                        "version": 0,
                    }
                try:
                    merge_reported(
                        document["state"]["reported"],
                        document["metadata"]["reported"],
                        state["reported"],
                        now,
                    )
                except RecursionError:
                    return rejection(400, "State nesting exceeds diagnostic limit")
                document["version"] += 1
                document["timestamp"] = now
                encoded = json.dumps(document, ensure_ascii=True, allow_nan=False)
                if len(encoded.encode()) > 256 * 1024:
                    return rejection(413, "Stored shadow exceeds diagnostic limit")
                db.execute("INSERT OR REPLACE INTO shadows VALUES (?, ?)", (base, encoded))
                if base == f"$aws/things/{client_id}/shadow/name/ro-currentstate":
                    from roomba_cloud.maps.live_position import reset_mission

                    reset_mission(
                        db, client_id, document["state"]["reported"].get("cleanMissionStatus", {})
                    )
                result = {
                    "state": state,
                    "metadata": {"reported": {}},
                    "version": document["version"],
                    "timestamp": now,
                }
                for key in state["reported"]:
                    if key in document["metadata"]["reported"]:
                        result["metadata"]["reported"][key] = document["metadata"]["reported"][key]
            if token is not None:
                result["clientToken"] = token
            return topic + "/accepted", result
