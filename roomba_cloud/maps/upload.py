#!/usr/bin/env python3
"""Negotiate robot uploads over MQTT; receive their bytes over shared HTTPS.

filexfer_req/filexfer_resp exchanges bind a random 15-minute token to the MQTT
robot ID, transfer type and xferId. The token URL authorizes a PUT without the
customer bearer key; it is not bound to source IP (multiple robots may share NAT).
Only negotiate while the HTTPS receiver's heartbeat is fresh (<=15 seconds),
and only when MQTT observed a response-topic subscription. A mission transfer
may contain just a mission report; accepting it does not imply map availability.

Pending -> receiving -> complete/failed is one-shot; retries cannot reuse an
already claimed token. Reserve declared bytes transactionally against per-file/
total limits. Fsync archive and directory before publishing completed metadata.
Geometry verification, edit-lock release, pruning and customer wakeup follow
completion; they must not turn partially stored bytes into a usable map.

Pruning is deliberately narrower than ingestion: only verified type-3 smart-map
archives fully superseded by newer archives for the SAME robot are removable.
Mission archives may supply type-21 geometry but are not pruned by this policy.
Tombstone before unlinking, coordinate with catalog read transactions, and retry
interrupted pruning on receiver heartbeats. Retained archives are private evidence,
not a supported way to restore maps deleted on the robot.
"""

import contextlib
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import tarfile
import time
import zlib
from urllib.parse import urlsplit

from roomba_cloud.shadow_service import finite_float, reject_constant
from roomba_cloud.cloud_http import log


class UploadRejected(ValueError):
    def __init__(self, status, reason):
        super().__init__(reason)
        self.status = status


class MapUploadService:
    """Coordinate MQTT and HTTPS processes through private SQLite and sibling archives."""

    TYPES = ("uploadP2MapSmart", "uploadP2MapMission", "uploadP2MapLive")

    def __init__(self, path, base_url=None):
        self.path = Path(path)
        if self.path.parent.stat().st_mode & 0o077:
            raise ValueError("Map upload database parent must be private (chmod 700)")
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            info = self.path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise ValueError(
                    "Map upload database must be a regular private mode-0600 file"
                ) from None
        else:
            os.close(fd)
        self.base_url = base_url
        if base_url is not None:
            url = urlsplit(base_url)
            if (
                url.scheme != "https"
                or not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
                or url.path not in ("", "/")
            ):
                raise ValueError(
                    "Map upload base URL must be an HTTPS origin without credentials/path/query"
                )
            if url.port is not None and not 1 <= url.port <= 65535:
                raise ValueError("Invalid map upload URL port")
            self.base_url = base_url.rstrip("/")
        with self.connect() as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS uploads ("
                "token TEXT PRIMARY KEY, robot_id TEXT NOT NULL, xfer_type TEXT NOT NULL,"
                "xfer_id INTEGER NOT NULL, expires INTEGER NOT NULL, status TEXT NOT NULL,"
                "bytes INTEGER NOT NULL DEFAULT 0, sha256 TEXT)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS receiver ("
                "id INTEGER PRIMARY KEY CHECK(id=1), heartbeat INTEGER NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS smart_archive_order ("
                "sequence INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT UNIQUE NOT NULL)"
            )

    def connect(self):
        return contextlib.closing(sqlite3.connect(self.path, timeout=10))

    def heartbeat(self, online=True):
        with self.connect() as db, db:
            db.execute(
                "INSERT OR REPLACE INTO receiver VALUES (1, ?)",
                (int(time.time()) if online else 0,),
            )
        if online:
            self.finish_pruning()

    def respond(self, topic, payload, robot_id):
        """Validate a supported negotiation and reuse only a still-pending correlation.

        Returning None means an unrelated topic/unsupported transfer, not a
        successful empty map. A missing/unready HTTPS receiver is an error.
        """
        if topic != f"v011-irbthbu/things/{robot_id}/filexfer_req":
            return None
        if self.base_url is None:
            raise ValueError("Map upload negotiation requires an HTTPS base URL")
        if len(payload) > 256 * 1024:
            raise ValueError("Map transfer request exceeds size limit")
        try:
            value = json.loads(payload, parse_constant=reject_constant, parse_float=finite_float)
        except RecursionError as exc:
            raise ValueError("Map transfer request nesting exceeds limit") from exc
        if not isinstance(value, dict):
            raise ValueError("Map transfer request must be an object")
        kind = value.get("xferType")
        if kind not in self.TYPES:
            log("map_upload_request_ignored", reason="unsupported transfer type")
            return None
        identifier = value.get("xferId")
        operation = value.get(kind)
        if not isinstance(operation, dict):
            raise ValueError("Invalid map transfer request schema")
        maps = operation.get("p2maps")
        mission_only = (
            kind == "uploadP2MapMission"
            and "p2maps" not in operation
            and isinstance(operation.get("missionId"), str)
            and 0 < len(operation["missionId"]) <= 128
            and type(operation.get("nMssn")) is int
            and operation["nMssn"] >= 0
        )
        if (
            type(identifier) is not int
            or not 0 <= identifier <= 2147483647
            or (
                not mission_only
                and (
                    not isinstance(maps, list)
                    or not 1 <= len(maps) <= 10
                    or any(
                        not isinstance(item, dict)
                        or any(
                            not isinstance(item.get(key), str)
                            or not item[key]
                            or len(item[key]) > 128
                            for key in ("p2map_id", "p2mapv_id")
                        )
                        for item in maps
                    )
                )
            )
        ):
            raise ValueError("Invalid map transfer request schema")
        now = int(time.time())
        with self.connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            heartbeat = db.execute("SELECT heartbeat FROM receiver WHERE id=1").fetchone()
            if heartbeat is None or not 0 <= now - heartbeat[0] <= 15:
                raise ValueError("Map upload receiver is not reporting ready")
            existing = db.execute(
                "SELECT token FROM uploads WHERE robot_id=? AND xfer_type=? AND xfer_id=? "
                "AND status='pending' AND expires>=?",
                (robot_id, kind, identifier, now),
            ).fetchone()
            if existing:
                token = existing[0]
            else:
                if db.execute("SELECT count(*) FROM uploads").fetchone()[0] >= 1000:
                    raise ValueError("Map upload request count limit reached")
                token = secrets.token_hex(32)
                db.execute(
                    "INSERT INTO uploads "
                    "(token,robot_id,xfer_type,xfer_id,expires,status) VALUES (?,?,?,?,?,'pending')",
                    (token, robot_id, kind, identifier, now + 900),
                )
        return topic.rsplit("/", 1)[0] + "/filexfer_resp", {
            "status": "success",
            "reqParams": {"xferType": kind, "xferId": identifier},
            "url": self.base_url + "/map-upload/" + token,
        }

    def claim(self, token, length, max_bytes, max_total):
        """Atomically reserve storage and consume a pending bearer URL before writing."""
        now = int(time.time())
        if not 0 < length <= max_bytes:
            raise UploadRejected(413, "Upload size outside allowed range")
        with self.connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT status,expires FROM uploads WHERE token=?", (token,)
            ).fetchone()
            if row is None:
                raise UploadRejected(404, "Unknown upload")
            if row[1] < now:
                raise UploadRejected(410, "Upload expired")
            if row[0] != "pending":
                raise UploadRejected(409, "Upload token already used")
            used = db.execute("SELECT coalesce(sum(bytes),0) FROM uploads").fetchone()[0]
            if used + length > max_total:
                raise UploadRejected(413, "Total upload storage limit reached")
            db.execute(
                "UPDATE uploads SET status='receiving',bytes=? WHERE token=?", (length, token)
            )

    def finish(self, token, status, digest=None):
        """Publish storage outcome; sequence smart archives by completion, not request time."""
        with self.connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            if status == "complete":
                db.execute(
                    "INSERT OR IGNORE INTO smart_archive_order(token) "
                    "SELECT token FROM uploads WHERE status='complete' "
                    "AND xfer_type='uploadP2MapSmart' ORDER BY rowid"
                )
            db.execute(
                "UPDATE uploads SET status=?,sha256=? WHERE token=?", (status, digest, token)
            )
            if status == "complete":
                db.execute(
                    "INSERT OR IGNORE INTO smart_archive_order(token) "
                    "SELECT token FROM uploads WHERE token=? AND xfer_type='uploadP2MapSmart'",
                    (token,),
                )

    def prune_smart_maps(self, token):
        """Retire only archives whose entire map-ID set is covered by newer verified data.

        Invalid archives are logged/skipped rather than counted as replacement
        coverage. Never remove another robot's archive or a unique older map.
        """
        from roomba_cloud.maps.catalog import read_archive

        failures = (OSError, ValueError, TypeError, KeyError, tarfile.TarError, zlib.error)

        def identities(row):
            identifier, size, digest = row
            if not re.fullmatch(r"[a-f0-9]{64}", identifier):
                raise ValueError("Invalid stored archive token")
            path = self.path.parent / (identifier + ".tar.gz")
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size != size:
                raise ValueError("Invalid stored smart-map archive")
            maps = read_archive(str(path), size, info.st_mtime_ns, digest, smart_only=True)
            ids = {map_id for map_id, _, _ in maps}
            if not ids or len(ids) != len(maps):
                raise ValueError("Missing or duplicate saved map identities")
            return ids

        with self.connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT token,bytes,sha256,robot_id FROM uploads "
                "WHERE token=? AND status='complete' AND xfer_type='uploadP2MapSmart'",
                (token,),
            ).fetchone()
            if current is None:
                return
            try:
                identities(current[:3])
            except failures as exc:
                log(
                    "map_archive_prune_skipped",
                    error_type=type(exc).__name__,
                    reason="new smart map could not be verified",
                )
                return
            rows = db.execute(
                "SELECT u.token,u.bytes,u.sha256 FROM uploads u "
                "LEFT JOIN smart_archive_order o ON o.token=u.token WHERE u.robot_id=? "
                "AND u.status='complete' AND u.xfer_type='uploadP2MapSmart' "
                "ORDER BY o.sequence IS NOT NULL DESC,o.sequence DESC,u.rowid DESC",
                (current[3],),
            ).fetchall()
            covered = set()
            for row in rows:
                try:
                    ids = identities(row)
                except failures as exc:
                    log(
                        "map_archive_prune_skipped",
                        error_type=type(exc).__name__,
                        reason="older smart map could not be verified",
                    )
                    continue
                if ids <= covered:
                    db.execute("UPDATE uploads SET status='pruning' WHERE token=?", (row[0],))
                else:
                    covered.update(ids)
        self.finish_pruning()

    def finish_pruning(self):
        """Finish durable tombstones idempotently after crashes/unlink failures."""
        with self.connect() as db:
            rows = db.execute("SELECT token FROM uploads WHERE status='pruning'").fetchall()
        for (token,) in rows:
            if not re.fullmatch(r"[a-f0-9]{64}", token):
                raise ValueError("Invalid pruning token")
            try:
                (self.path.parent / (token + ".tar.gz")).unlink(missing_ok=True)
                directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
                with self.connect() as db, db:
                    db.execute("DELETE FROM smart_archive_order WHERE token=?", (token,))
                    db.execute("DELETE FROM uploads WHERE token=? AND status='pruning'", (token,))
                log("map_archive_pruned")
            except (OSError, sqlite3.Error) as exc:
                log(
                    "map_archive_prune_failed",
                    error_type=type(exc).__name__,
                    recovery="retry on next receiver heartbeat",
                )


class MapUploadHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        # Request paths contain bearer tokens.
        pass

    def reply(self, status):
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.close_connection = True

    def do_PUT(self):
        """Receive a bounded fixed-length upload without exposing bearer URLs in logs.

        Reject chunked/ambiguous framing; enforce a 30-second read timeout and
        five-minute overall limit. Release matching edit locks before pruning
        can discard the proof archive, then notify telemetry after persistence.
        """
        self.connection.settimeout(30)
        match = re.fullmatch(r"/map-upload/([0-9a-f]{64})", self.path)
        if not match:
            log("map_upload_rejected", reason="invalid upload path")
            self.reply(404)
            return
        lengths = self.headers.get_all("Content-Length", [])
        if (
            self.headers.get("Transfer-Encoding")
            or len(lengths) != 1
            or not re.fullmatch(r"[0-9]{1,12}", lengths[0])
        ):
            log("map_upload_rejected", reason="requires one bounded Content-Length")
            self.reply(400)
            return
        token = match[1]
        service = self.server.uploads
        try:
            length = int(lengths[0])
            service.claim(token, length, self.server.max_bytes, self.server.max_total)
        except UploadRejected as exc:
            log("map_upload_rejected", reason=str(exc), status=exc.status)
            self.reply(exc.status)
            return
        except (OSError, sqlite3.Error) as exc:
            log("map_upload_failed", error_type=type(exc).__name__)
            self.reply(500)
            return
        target = service.path.parent / (token + ".tar.gz")
        claimed_at = time.monotonic()
        created = False
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
            digest = hashlib.sha256()
            with os.fdopen(fd, "wb") as output:
                remaining = length
                while remaining:
                    timeout = min(30, 300 - (time.monotonic() - claimed_at))
                    if timeout <= 0:
                        raise TimeoutError("Upload exceeded total time limit")
                    self.connection.settimeout(timeout)
                    chunk = self.rfile.read1(min(65536, remaining))
                    if not chunk:
                        raise ValueError("Truncated upload")
                    output.write(chunk)
                    digest.update(chunk)
                    remaining -= len(chunk)
                output.flush()
                os.fsync(output.fileno())
            with contextlib.ExitStack() as stack:
                directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
                stack.callback(os.close, directory)
                os.fsync(directory)
            received_at = time.time()
            service.finish(token, "complete", digest.hexdigest())
            log("map_upload_stored", bytes=length, sha256=digest.hexdigest())
        except (OSError, ValueError, sqlite3.Error) as exc:
            log("map_upload_failed", error_type=type(exc).__name__)
            if created:
                target.unlink()
            service.finish(token, "failed")
            self.reply(500)
            return
        shadow_db = getattr(self.server, "customer_shadow_db", None)
        if shadow_db is not None:
            from roomba_cloud.maps.room_edits import clear_uploaded_locks

            try:
                clear_uploaded_locks(shadow_db, service.path, token, received_at)
            except (OSError, ValueError, sqlite3.Error, tarfile.TarError, zlib.error) as exc:
                log("map_edit_unlock_failed", error_type=type(exc).__name__)
        try:
            service.prune_smart_maps(token)
        except (OSError, sqlite3.Error) as exc:
            log("map_archive_prune_failed", error_type=type(exc).__name__)
        if shadow_db is not None:
            from roomba_cloud.telemetry_events import TelemetryNotifier

            with service.connect() as db:
                row = db.execute("SELECT robot_id FROM uploads WHERE token=?", (token,)).fetchone()
            TelemetryNotifier(shadow_db).notify(row[0])
        self.reply(200)


class MapUploadServer(ThreadingHTTPServer):
    """Shared HTTPS listener; the historical log prefix does not identify an upload.

    TLS fails before any URL is available. Correlate peer/time with MQTT
    negotiation before attributing map_upload_tls_failed to a robot transfer.
    UNKNOWN_CA can mean server-chain rejection; timeouts/resets/non-TLS traffic
    have other causes. Log structured OpenSSL/OS fields, not request secrets or
    raw exception strings.
    """

    daemon_threads = True
    block_on_close = False

    def get_request(self):
        raw, peer = self.socket.accept()
        raw.settimeout(10)
        try:
            return self.tls_context.wrap_socket(raw, server_side=True), peer
        except (OSError, ValueError) as exc:
            raw.close()
            log(
                "map_upload_tls_failed",
                peer=peer[0],
                port=peer[1],
                error_type=type(exc).__name__,
                reason=getattr(exc, "reason", None),
                library=getattr(exc, "library", None),
                errno=getattr(exc, "errno", None),
                message="TLS handshake failed before HTTP request; endpoint and transfer type unknown",
            )
            raise

    def service_actions(self):
        self.uploads.heartbeat()

    def handle_error(self, request, client_address):
        log("map_upload_handler_failed", message="Check receiver storage and database locally")


if __name__ == "__main__":
    raise SystemExit(
        "Use cloud_http.py --map-upload-db to serve uploads alongside discovery/registration"
    )
