"""Private HTTPS->MQTT IPC for allowlisted one-shot commands.

commands.sock is a private Unix socket in shared state; one bounded JSON line
in, one JSON line out. The MQTT process owns active sessions, while HTTPS only
requests work. No disconnected queue, reconnect replay or automatic retries.
Connection/ACK failures can leave execution uncertain even when the caller gets
an error. Matter or fresh robot reports are the execution confirmation source.

Separate command families deliberately use separate topics: /cmd for mission
verbs, settings shadow /delta for configuration, /livemap/cmd for a bounded live
telemetry request, /edit_req for robot-backed map edits. Internal firmware
service names are not automatically valid cloud commands.
"""

import json
import errno
import os
from pathlib import Path
import re
import socket
import socketserver
import stat
import sqlite3
import tarfile
import threading
import time
import zlib
from collections.abc import Callable
from os import PathLike
from typing import Literal, TypedDict

from roomba_cloud.robot_mqtt import mqtt_string, packet


class CommandError(Exception):
    pass


COMMANDS = {"find", "start", "pause", "stop", "dock"}
DOCK_COMMANDS = {"evac", "drypad", "stoppaddry", "washpad"}
# Verified V4 translator: evac -> station action 3/on, drypad -> 2/on,
# stoppaddry -> 2/off, washpad -> 1/on. Dock actions require the robot to be docked.


class DeliveryResult(TypedDict):
    """PUBACK receipt only; never a claim that firmware carried out the action."""

    delivery: Literal["acknowledged"]
    execution: Literal["unconfirmed"]


class EditReceipt(TypedDict):
    """Durable edit correlation; callers poll room_edits instead of assuming success."""

    transaction_id: int


class CommandSession:
    """Serialize writes and permit one outstanding QoS-1 operation per robot session.

    send_lock protects MQTT frame writes shared with the reader's responses.
    command_lock prevents overlapping packet-ID/ACK state. The reader must remain
    running while a command waits, because it delivers acknowledge() callbacks.
    A closed/replaced session wakes waiters but must never replay their commands.
    """

    def __init__(self, conn, version, robot_id):
        self.conn = conn
        self.version = version
        self.topic = f"v011-irbthbu/things/{robot_id}/cmd"
        self.settings_topic = f"$aws/things/{robot_id}/shadow/name/rw-settings/update/delta"
        self.live_map_topic = f"v011-irbthbu/things/{robot_id}/livemap/cmd"
        self.edit_topic = f"v011-irbthbu/things/{robot_id}/edit_req"
        self.subscriptions: set[str] = set()
        self.send_lock = threading.Lock()
        self.command_lock = threading.Lock()
        self.ack = threading.Event()
        self.identifier = 0
        self.pending: int | None = None
        self.reason: int | None = None
        self.closed = False
        self.map_bootstrap_attempted = False
        self.map_refresh_in_progress = False

    def __getattr__(self, name):
        return getattr(self.conn, name)

    def sendall(self, data):
        with self.send_lock:
            self.conn.sendall(data)

    def acknowledge(self, identifier, reason):
        if identifier == self.pending:
            self.reason = reason
            self.ack.set()

    def close(self):
        self.closed = True
        self.ack.set()

    def locate(self):
        return self.execute("find")

    def execute(
        self,
        command: str,
        room_target: dict[str, object] | None = None,
        operating_mode: int | None = None,
    ) -> DeliveryResult:
        if command not in COMMANDS:
            raise CommandError("Unsupported robot command")
        if room_target is not None and command != "start":
            raise CommandError("Room targets are only valid for start")
        if operating_mode is not None and (
            command != "start" or type(operating_mode) is not int or operating_mode not in (2, 4, 6)
        ):
            raise CommandError("Unsupported cleaning mode")
        return self.publish_once(
            self.topic,
            lambda: {
                "command": command,
                "time": int(time.time()),
                "initiator": "localApp",
                **(room_target or {}),
                **(
                    {"params": {"operatingMode": operating_mode}}
                    if operating_mode is not None
                    else {}
                ),
            },
        )

    def publish_once(self, topic: str, document: Callable[[], object]) -> DeliveryResult:
        """Build after subscription/availability checks, send once, await matching PUBACK.

        The callback performs last-moment mission/settings checks under command
        ownership. A timeout after sending is uncertain, not permission to retry.
        MQTT 5 adds an empty properties byte; both versions correlate packet IDs.
        """
        from roomba_cloud.mqtt_server import topic_matches

        if not self.command_lock.acquire(blocking=False):
            raise CommandError("Another command is in progress")
        try:
            if self.closed or not any(topic_matches(s, topic) for s in tuple(self.subscriptions)):
                raise CommandError(
                    "Robot is disconnected or not subscribed to the requested command/settings topic"
                )
            self.identifier = self.identifier % 65535 + 1
            self.pending = self.identifier
            self.reason = None
            self.ack.clear()
            payload = json.dumps(document(), separators=(",", ":")).encode()
            wire = mqtt_string(topic) + self.pending.to_bytes(2, "big")
            if self.version == 5:
                wire += b"\0"
            self.sendall(packet(0x32, wire + payload))
            if not self.ack.wait(5):
                raise CommandError("Delivery acknowledgment timed out; execution is uncertain")
            if self.closed:
                raise CommandError("Robot disconnected; execution is uncertain")
            if self.reason is None or self.reason >= 128:
                raise CommandError("Robot rejected MQTT delivery")
            return {"delivery": "acknowledged", "execution": "unconfirmed"}
        finally:
            self.pending = None
            self.command_lock.release()


class CommandRegistry:
    """Route by asserted robot ID, not source IP; retain only each newest session."""

    def __init__(
        self,
        shadow_db: str | PathLike[str] | None = None,
        map_upload_db: str | PathLike[str] | None = None,
    ) -> None:
        self.lock = threading.Lock()
        self.sessions: dict[str, CommandSession] = {}
        self.shadow_db = shadow_db
        self.map_upload_db = map_upload_db
        if shadow_db is not None:
            from roomba_cloud.maps.room_edits import initialize

            initialize(shadow_db)

    def register(self, robot_id: str, session: CommandSession) -> None:
        with self.lock:
            old = self.sessions.get(robot_id)
            if old is not None:
                old.close()
            self.sessions[robot_id] = session

    def remove(self, robot_id: str, session: CommandSession) -> None:
        """Do not let an old connection's finally block unregister its replacement."""
        session.close()
        with self.lock:
            if self.sessions.get(robot_id) is session:
                del self.sessions[robot_id]

    def execute(self, request: object) -> DeliveryResult | EditReceipt:
        """Validate exact IPC shape and robot capabilities before topic-specific dispatch.

        Booleans are not numeric modes/durations. Room targets must resolve against
        current inventory/archives; settings and edits perform their own validation
        again at payload construction rather than trusting prior HTTP/UI checks.
        """
        rooms = isinstance(request, dict) and request.get("command") == "clean_segments"
        settings = isinstance(request, dict) and request.get("command") == "set_setting"
        live_map = isinstance(request, dict) and request.get("command") == "live_map"
        room_edit = isinstance(request, dict) and request.get("command") == "room_edit"
        keys = (
            {"robot_id", "command", "edit"}
            if room_edit
            else {"robot_id", "command", "duration"}
            if live_map
            else {"robot_id", "command", "key", "value"}
            if settings
            else {"robot_id", "command", "segment_ids"}
            if rooms
            else {"robot_id", "command"}
        )
        if isinstance(request, dict) and "operating_mode" in request:
            keys = keys | {"operating_mode"}
        if (
            not isinstance(request, dict)
            or set(request) != keys
            or not isinstance(request["command"], str)
            or request["command"]
            not in COMMANDS
            | DOCK_COMMANDS
            | {"clean_segments", "set_setting", "live_map", "room_edit", "train"}
            or not isinstance(request["robot_id"], str)
            or not re.fullmatch(r"[0-9A-Fa-f]{32}", request["robot_id"])
        ):
            raise CommandError("Only allowlisted commands with a valid robot ID are supported")
        if live_map and (
            type(request["duration"]) is not int or not 1 <= request["duration"] <= 300
        ):
            raise CommandError("Live-map duration must be an integer from 1 to 300 seconds")
        mode = request.get("operating_mode")
        if "operating_mode" in request:
            from roomba_cloud.robot_settings import cleaning_modes

            if (
                request["command"] not in ("start", "clean_segments")
                or type(mode) is not int
                or self.shadow_db is None
                or mode not in cleaning_modes(self.shadow_db, request["robot_id"])
            ):
                raise CommandError("Unsupported cleaning mode for this robot")
        with self.lock:
            session = self.sessions.get(request["robot_id"])
        if session is None:
            raise CommandError("No active robot MQTT session")
        if room_edit:
            if self.shadow_db is None or self.map_upload_db is None:
                raise CommandError("Room editing is not enabled")
            from roomba_cloud.maps.room_edits import submit

            try:
                return submit(
                    self.shadow_db,
                    self.map_upload_db,
                    request["robot_id"],
                    session,
                    request["edit"],
                )
            except ValueError as exc:
                raise CommandError(str(exc)) from exc
        if request["command"] in DOCK_COMMANDS | {"train"}:
            from roomba_cloud.maps.controls import map_control_state

            def guarded_payload():
                if self.shadow_db is None:
                    raise CommandError("Robot state is unavailable")
                state = map_control_state(
                    self.shadow_db,
                    request["robot_id"],
                    docked_only=request["command"] in DOCK_COMMANDS,
                )
                if not state["allowed"]:
                    raise CommandError(state["reason"])
                return {
                    "command": request["command"],
                    "time": int(time.time()),
                    "initiator": "localApp",
                }

            return session.publish_once(session.topic, guarded_payload)
        if live_map:
            return session.publish_once(
                session.live_map_topic,
                lambda: {"operation": "start", "start": {"duration": request["duration"]}},
            )
        if settings:
            if self.shadow_db is None:
                raise CommandError("Settings are not enabled")
            from roomba_cloud.shadow_service import ShadowService

            shadows = ShadowService(self.shadow_db)
            return session.publish_once(
                session.settings_topic,
                lambda: shadows.setting_delta(
                    request["robot_id"], request["key"], request["value"]
                ),
            )
        if rooms:
            if self.shadow_db is None or self.map_upload_db is None:
                raise CommandError("Room catalog is not enabled")
            from roomba_cloud.maps.catalog import room_command

            target = room_command(
                self.shadow_db, self.map_upload_db, request["robot_id"], request["segment_ids"]
            )
            return session.execute("start", target, mode)
        return session.execute(request["command"], operating_mode=mode)


class CommandHandler(socketserver.StreamRequestHandler):
    server: "CommandServer"

    def handle(self):
        self.connection.settimeout(8)
        try:
            raw = self.rfile.readline(65537)
            if not raw:
                from roomba_cloud.cloud_http import log

                log("robot_command_peer_closed", request_received=False)
                return
            if len(raw) > 65536 or not raw.endswith(b"\n"):
                raise CommandError("Invalid command framing")
            result = self.server.registry.execute(json.loads(raw))
        except (
            CommandError,
            OSError,
            ValueError,
            sqlite3.Error,
            tarfile.TarError,
            zlib.error,
        ) as exc:
            from roomba_cloud.cloud_http import log

            log("robot_command_failed", error_type=type(exc).__name__)
            result = {
                "error": str(exc) if isinstance(exc, CommandError) else "Command transport failed"
            }
        self.wfile.write(json.dumps(result).encode() + b"\n")


class CommandServer(socketserver.ThreadingUnixStreamServer):
    """Recover only a refused stale owned socket; never steal an active listener.

    Remember the bound inode so cleanup cannot unlink a replacement socket.
    Both containers must share the same private path and runtime user.
    """

    daemon_threads = True

    def __init__(self, path, registry):
        path = Path(path)
        if path.parent.stat().st_mode & 0o077:
            raise ValueError("Command socket directory must be private (mode 0700)")
        self.path = path
        self.registry = registry
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if (
                not stat.S_ISSOCK(info.st_mode)
                or info.st_mode & 0o077
                or info.st_uid != os.getuid()
            ):
                raise ValueError("Existing command socket must be a private owned socket")
            with socket.socket(socket.AF_UNIX) as probe:
                probe.settimeout(1)
                try:
                    probe.connect(str(path))
                except OSError as exc:
                    if exc.errno != errno.ECONNREFUSED:
                        raise
                else:
                    raise ValueError("Command socket already has a listener")
            path.unlink()
        super().__init__(str(path), CommandHandler)
        os.chmod(path, 0o600)
        self._identity = path.stat().st_ino

    def server_close(self):
        super().server_close()
        if self.path.exists() and self.path.stat().st_ino == self._identity:
            self.path.unlink()


def request_command(
    path: str | PathLike[str],
    robot_id: str,
    command: str = "find",
    segment_ids: object = None,
    setting: object = None,
    operating_mode: object = None,
    duration: object = None,
    edit: object = None,
) -> DeliveryResult | EditReceipt:
    """Send a single bounded IPC request and validate the entire response shape.

    Inputs are objects because this is an untrusted API boundary, not permission
    to bypass validation. Malformed/non-object responses are explicit errors.
    The eight-second IPC timeout exceeds the session's five-second ACK wait;
    neither timeout establishes that an already written action did not execute.
    """
    if command not in COMMANDS | DOCK_COMMANDS | {
        "clean_segments",
        "set_setting",
        "live_map",
        "room_edit",
        "train",
    }:
        raise CommandError("Unsupported robot command")
    request: dict[str, object] = {"robot_id": robot_id, "command": command}
    if command == "room_edit":
        request["edit"] = edit
    elif edit is not None:
        raise CommandError("Edit parameters require a room edit")
    if command == "live_map":
        if type(duration) is not int or not 1 <= duration <= 300:
            raise CommandError("Live-map duration must be an integer from 1 to 300 seconds")
        request["duration"] = duration
    elif duration is not None:
        raise CommandError("Duration requires a live-map request")
    if operating_mode is not None:
        if (
            command not in ("start", "clean_segments")
            or type(operating_mode) is not int
            or operating_mode not in (2, 4, 6)
        ):
            raise CommandError("Unsupported cleaning mode")
        request["operating_mode"] = operating_mode
    if command == "set_setting":
        if not isinstance(setting, dict) or set(setting) != {"key", "value"}:
            raise CommandError("Invalid setting request")
        request.update(setting)
    elif setting is not None:
        raise CommandError("Settings require set_setting")
    if command == "clean_segments":
        request["segment_ids"] = segment_ids
    elif segment_ids is not None:
        raise CommandError("Segments require a room-cleaning command")
    with socket.socket(socket.AF_UNIX) as conn:
        conn.settimeout(8)
        conn.connect(str(path))
        conn.sendall(json.dumps(request).encode() + b"\n")
        with conn.makefile("rb") as source:
            raw = source.readline(1025)
        if len(raw) > 1024 or not raw.endswith(b"\n"):
            raise CommandError("Invalid command service response")
        result: object = json.loads(raw)
        if not isinstance(result, dict):
            raise CommandError("Invalid command service response")
        if "error" in result:
            if set(result) != {"error"} or not isinstance(result["error"], str):
                raise CommandError("Invalid command service error")
            raise CommandError(result["error"])
        if command == "room_edit":
            if (
                not isinstance(result, dict)
                or set(result) != {"transaction_id"}
                or type(result["transaction_id"]) is not int
                or result["transaction_id"] < 1
            ):
                raise CommandError("Invalid room edit result")
            return {"transaction_id": result["transaction_id"]}
        elif result != {"delivery": "acknowledged", "execution": "unconfirmed"}:
            raise CommandError("Invalid delivery result")
        return {"delivery": "acknowledged", "execution": "unconfirmed"}
