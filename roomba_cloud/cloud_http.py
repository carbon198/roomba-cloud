#!/usr/bin/env python3
"""Local HTTPS listener for robot discovery/registration/uploads and customer auth.

Serves discovery and registration with a trusted certificate and TLS >= 1.2.
Robot routes accept LAN clients; customer routes require a private bearer token.
Registration acknowledges locally without verifying robot identity. Request
secrets are redacted and non-JSON bodies omitted. Never forwards traffic.
--customer-token-file enables pre-shared bearer authentication for customer
requests and GET /v1/customer/session. --customer-shadow-db additionally enables
sanitized read-only telemetry for the native HA integration.

Use python -m roomba_cloud for deployments. The public /, /dashboard.js and /dashboard.css
contain no robot data; all inventory, telemetry, map, editor and control routes
are authenticated when configured. The shared customer key grants access to ALL
robots, not separate tenants. Upload PUTs use short-lived negotiation tokens
(map_upload), not customer auth. Registration is an acknowledgement/history
record, not cryptographic enrollment; discovery-only peers have no robot ID.
No local response verifies registration against iRobot or provisions firmware.

Customer route families:
* /v1/customer/robots and /session: sanitized inventory/authentication.
* /robots/<id>/telemetry, /events, /maps/...: snapshots, SSE and revisioned PNGs.
* /robots/<id>/locate, /commands/<verb>, /settings/<key>: one-shot command IPC.
* /robots/<id>/room-editor, /room-edits[/<transaction>]: source geometry/edits.
Methods, fixed body lengths and exact command fields are validated per route.
Names are validated before IPC and again by room_edits. Application errors
must not masquerade as successful empty inventories or confirmed execution.

Never enable payload/secret diagnostics on a public or shared system. Customer
auth/body failures log types/safe context, not bearer keys or raw exception text.
The historical low-level handle/read_http functions are diagnostic helpers,
not the configured threaded request path returned by cloud_handler.
"""

import argparse
import contextlib
import hmac
import json
import os
import re
import socket
import sqlite3
import ssl
import stat
import struct
import subprocess
import sys
import tarfile
import time
import zlib
from pathlib import Path
from urllib.parse import urlsplit

SHOW_SECRETS = False
REDACT = {"authorization", "x-amz-security-token", "cookie", "x-irbt-auth", "x-api-key"}
BODY_SECRETS = (
    "password",
    "token",
    "certificate",
    "signature",
    "secret",
    "credential",
    "authorization",
    "cookie",
    "privatekey",
    "apikey",
)


class CustomerAuth:
    """Load a private regular non-symlink token and compare one Bearer header.

    Bounded reads avoid special/oversized files. Duplicate Authorization headers
    are rejected rather than combined; secrets stay out of URLs/cookies/logs.
    The optional trailing line ending permits local token-file tooling.
    """

    def __init__(self, token):
        self._token = token

    @classmethod
    def load(cls, path):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise ValueError("Customer token must be a private regular file (mode 0600)")
            raw = source.read(131)
            token = raw.removesuffix(b"\n").removesuffix(b"\r")
        if len(raw) > 130 or not re.fullmatch(rb"[A-Za-z0-9_-]{43,128}", token):
            raise ValueError("Customer token must contain 43-128 URL-safe characters")
        return cls(token)

    def accepts(self, values):
        if len(values) != 1:
            return False
        match = re.fullmatch(r"Bearer ([A-Za-z0-9_-]{43,128})", values[0], re.IGNORECASE)
        return bool(match and hmac.compare_digest(match[1].encode("ascii"), self._token))


def robot_route(path):
    return path == "/v1/robot/discover" or bool(re.fullmatch(r"/v1/[0-9A-Fa-f]{32}/register", path))


def log(event: str, **fields: object) -> None:
    """Emit structured context; callers must supply only already-redacted safe fields."""
    print(json.dumps({"t": round(time.time(), 3), "event": event, **fields}), flush=True)


def parse_client_hello(data):
    """Best-effort ClientHello summary from a peeked TLS record; never raises."""
    info = {}
    try:
        if len(data) < 6 or data[0] != 0x16:
            return {"note": "not a TLS handshake record", "first_bytes": data[:8].hex()}
        pos = 5
        if data[pos] != 1:
            return {"note": "not a ClientHello"}
        pos += 4
        info["legacy_version"] = f"{data[pos]}.{data[pos + 1]}"
        pos += 2 + 32
        pos += 1 + data[pos]
        n = struct.unpack("!H", data[pos : pos + 2])[0]
        pos += 2
        info["cipher_suites"] = [
            f"{struct.unpack('!H', data[pos + i : pos + i + 2])[0]:04x}" for i in range(0, n, 2)
        ]
        pos += n
        pos += 1 + data[pos]
        ext_end = pos + 2 + struct.unpack("!H", data[pos : pos + 2])[0]
        pos += 2
        names = {0: "sni", 10: "groups", 13: "sigalgs", 16: "alpn", 43: "versions"}
        exts = []
        while pos + 4 <= min(ext_end, len(data)):
            etype, elen = struct.unpack("!HH", data[pos : pos + 4])
            body = data[pos + 4 : pos + 4 + elen]
            exts.append(etype)
            if etype == 0 and len(body) > 5:
                info["sni"] = body[5 : 5 + struct.unpack("!H", body[3:5])[0]].decode(
                    "ascii", "replace"
                )
            elif etype == 13 and len(body) >= 2:
                info["sigalgs"] = [body[i : i + 2].hex() for i in range(2, len(body), 2)]
            elif etype == 43 and len(body) >= 1:
                info["supported_versions"] = [body[i : i + 2].hex() for i in range(1, len(body), 2)]
            elif etype == 16:
                info["alpn"] = body[2:].decode("ascii", "replace")
            pos += 4 + elen
        info["extensions"] = [names.get(e, e) for e in exts]
        info["truncated"] = len(data) < ext_end
    except (IndexError, struct.error):
        info["note"] = "ClientHello parse incomplete"
    return info


def read_http(conn, limit=65536):
    """Read a bounded diagnostic request; production framing lives in cloud_handler."""
    data = b""
    conn.settimeout(10)
    while b"\r\n\r\n" not in data and len(data) < limit:
        chunk = conn.recv(8192)
        if not chunk:
            break
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    headers = {}
    raw_lines = []
    for line in lines[1:]:
        key, _, value = line.partition(":")
        hidden = key.strip().lower() in REDACT and not SHOW_SECRETS
        headers[key.strip().lower()] = "<redacted>" if hidden else value.strip()
        raw_lines.append(f"{key}: <redacted>" if hidden else line)
    length = (
        int(headers.get("content-length", "0") or 0)
        if headers.get("content-length", "0").isdigit()
        else 0
    )
    while len(rest) < min(length, limit):
        chunk = conn.recv(8192)
        if not chunk:
            break
        rest += chunk
    return lines[0], headers, rest[:limit], raw_lines


def redact_json(value):
    """Hide secret-like keys recursively, normalizing punctuation/case before matching."""
    if isinstance(value, dict):
        return {
            key: "<redacted>"
            if any(word in re.sub(r"[^a-z0-9]", "", key.lower()) for word in BODY_SECRETS)
            else redact_json(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_json(item) for item in value]
    return value


def logged_body(body):
    """Diagnostic JSON only by default; non-JSON/scalars stay omitted, not hex-dumped."""
    if not body:
        return {"body": ""}
    if SHOW_SECRETS:
        try:
            return {"body": body.decode("utf-8")}
        except UnicodeDecodeError:
            return {"body_hex": body.hex()}
    try:
        document = json.loads(body)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return {"body": "<omitted>", "body_note": "non-JSON or invalid JSON"}
    if not isinstance(document, (dict, list)):
        return {"body": "<omitted>", "body_note": "JSON scalar"}
    try:
        return {"body": json.dumps(redact_json(document))}
    except RecursionError:
        return {"body": "<omitted>", "body_note": "JSON nesting too deep"}


def http_response(request_line, discovery=None, registration_ok=False):
    """Implement only discovery/registration; unknown cloud APIs fail explicitly."""
    parts = request_line.split()
    if discovery is not None:
        if (
            len(parts) == 3
            and parts[0] == "GET"
            and urlsplit(parts[1]).path == "/v1/robot/discover"
        ):
            status = "200 OK"
            payload = discovery
        elif (
            registration_ok
            and len(parts) == 3
            and parts[0] == "POST"
            and re.fullmatch(r"/v1/[0-9A-Fa-f]{32}/register", urlsplit(parts[1]).path)
        ):
            status = "200 OK"
            payload = b"{}"
        else:
            status = "501 Not Implemented"
            payload = b'{"message":"endpoint not implemented"}'
    else:
        status = "404 Not Found"
        payload = b'{"message":"not found"}'
    response = (
        f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\n"
        "Cache-Control: no-store\r\nConnection: close\r\n"
        f"Content-Length: {len(payload)}\r\n\r\n"
    ).encode("ascii") + payload
    return status, response


def load_discovery(path):
    document = json.loads(Path(path).read_text())
    if not isinstance(document, dict):
        raise ValueError("Discovery response must be a JSON object")
    if not isinstance(document.get("httpBase"), str):
        raise ValueError("Discovery response requires a string httpBase")
    base = urlsplit(document["httpBase"])
    if base.scheme != "https" or not base.hostname:
        raise ValueError("Discovery httpBase must be an HTTPS URL")
    ttl = document.get("discoveryTTL")
    if type(ttl) is not int or ttl <= 0:
        raise ValueError("Discovery discoveryTTL must be a positive integer")
    return json.dumps(document, separators=(",", ":")).encode("utf-8")


def handle(raw, peer, context, discovery=None, registration_ok=False):
    try:
        raw.settimeout(10)
        try:
            hello = raw.recv(4096, socket.MSG_PEEK)
        except OSError as exc:
            log("peek_failed", peer=peer[0], error=str(exc))
            return
        log("client_hello", peer=peer[0], **parse_client_hello(hello))
        try:
            conn = context.wrap_socket(raw, server_side=True)
        except (ssl.SSLError, OSError) as exc:
            log("handshake_failed", peer=peer[0], error=str(exc))
            return
        with conn:
            log("handshake_ok", peer=peer[0], tls=conn.version(), cipher=conn.cipher()[0])
            try:
                request_line, headers, body, raw_lines = read_http(conn)
            except (OSError, ValueError) as exc:
                # With TLS 1.3 a client that rejects our certificate only tells us after
                # the server-side handshake has already "succeeded".
                hint = (
                    "client likely rejected the certificate"
                    if isinstance(exc, (ConnectionResetError, ssl.SSLError))
                    else "client sent no request"
                )
                log("no_http_request", peer=peer[0], error=str(exc), hint=hint)
                return
            if not request_line and not headers:
                log(
                    "client_closed_without_request",
                    peer=peer[0],
                    hint="handshake completed but the client sent nothing; under TLS 1.3 "
                    "this usually means it rejected the certificate (try --max-tls 1.2)",
                )
                return
            log(
                "http_request",
                peer=peer[0],
                request=request_line,
                headers=headers,
                raw_head=[request_line] + raw_lines,
                body_bytes=len(body),
                **logged_body(body),
            )
            status, response = http_response(request_line, discovery, registration_ok)
            conn.sendall(response)
            log("http_response", peer=peer[0], status=status)
    finally:
        raw.close()


def make_cert(directory, names):
    """Generate a short-lived local diagnostic certificate, not a robot-trusted chain."""
    cert, key = Path(directory) / "cert.pem", Path(directory) / "key.pem"
    san = ",".join(f"DNS:{n}" for n in names)
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "2",
            "-subj",
            f"/CN={names[0]}",
            "-addext",
            f"subjectAltName={san}",
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    return cert, key


def cloud_handler():
    """Build the shared upload/customer handler without a module-level import cycle.

    Several services lazily import this module's logger and one another's
    decoders. Keep imports at their use boundaries unless cycles are deliberately
    removed; moving them mechanically to module scope can break startup.
    """
    from roomba_cloud.maps.upload import MapUploadHandler

    class CloudHTTPHandler(MapUploadHandler):
        def do_GET(self):
            self.cloud_request()

        def do_POST(self):
            self.cloud_request()

        def do_PUT(self):
            if self.path.startswith("/map-upload/"):
                if self.server.uploads is None:
                    log("map_upload_rejected", reason="uploads disabled")
                    self.reply(404)
                else:
                    super().do_PUT()
            else:
                self.cloud_request()

        do_HEAD = do_GET
        do_DELETE = do_GET
        do_PATCH = do_GET
        do_OPTIONS = do_GET

        def cloud_request(self):
            """Dispatch assets, token PUT exclusions and authenticated APIs before diagnostics."""
            self.connection.settimeout(10)
            path = urlsplit(self.path).path
            if path == "/healthz":
                self.reply(200 if self.command == "GET" else 405)
                return
            from roomba_cloud.web.dashboard import ASSETS

            if path in ASSETS:
                if self.command != "GET":
                    self.reply(405)
                    return
                content_type, content = ASSETS[path]
                self.dashboard_reply(content.encode(), content_type)
                return
            if path.startswith("/map-upload"):
                log("map_upload_rejected", reason="unsupported method or path")
                self.reply(405)
                return
            auth = getattr(self.server, "customer_auth", None)
            if not robot_route(path) and (
                auth is not None or path in ("/v1/customer/session", "/v1/customer/robots")
            ):
                if auth is None:
                    log("customer_auth_rejected", reason="authentication disabled")
                    self.reply(503)
                    return
                if not auth.accepts(self.headers.get_all("Authorization", [])):
                    log(
                        "customer_auth_rejected",
                        reason="invalid or missing bearer token",
                        peer=self.client_address[0],
                    )
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Bearer realm="roomba-local"')
                    self.send_header("Content-Length", "0")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.close_connection = True
                    return
                log("customer_authenticated", peer=self.client_address[0], method=self.command)
                if path == "/v1/customer/robots":
                    if self.command != "GET":
                        self.reply(405)
                        return
                    db_path = getattr(self.server, "customer_shadow_db", None)
                    if db_path is None:
                        log("robot_inventory_failed", reason="shadow API disabled")
                        self.reply(503)
                        return
                    from roomba_cloud.web.dashboard import inventory

                    try:
                        payload = json.dumps(inventory(db_path), allow_nan=False).encode()
                    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError) as exc:
                        log("robot_inventory_failed", error_type=type(exc).__name__)
                        self.reply(500)
                        return
                    self.dashboard_reply(payload, "application/json")
                    return
                locate = re.fullmatch(r"/v1/customer/robots/([0-9A-Fa-f]{32})/locate", path)
                editor_route = re.fullmatch(
                    r"/v1/customer/robots/([0-9A-Fa-f]{32})/room-editor", path
                )
                edit_route = re.fullmatch(
                    r"/v1/customer/robots/([0-9A-Fa-f]{32})/room-edits(?:/([0-9]{1,10}))?", path
                )
                if edit_route:
                    # Transport delivery and edit acceptance are separate states.
                    # Do not retry IPC after timeout: the robot may have received it.
                    robot_id, transaction_id = edit_route.groups()
                    if self.command != ("GET" if transaction_id else "POST"):
                        self.reply(405)
                        return
                    db_path = getattr(self.server, "customer_shadow_db", None)
                    command_socket = getattr(self.server, "command_socket", None)
                    if db_path is None or command_socket is None:
                        self.reply(503)
                        return
                    from roomba_cloud.robot_commands import CommandError, request_command
                    from roomba_cloud.maps.room_edits import edit_status, normalize_room_name

                    try:
                        if transaction_id:
                            document = edit_status(db_path, robot_id, int(transaction_id))
                        else:
                            lengths = self.headers.get_all("Content-Length", [])
                            if (
                                self.headers.get("Transfer-Encoding")
                                or len(lengths) != 1
                                or not re.fullmatch(r"[0-9]{1,5}", lengths[0])
                                or not 1 <= int(lengths[0]) <= 8192
                            ):
                                raise ValueError("Invalid room edit request size")
                            raw = self.rfile.read(int(lengths[0]))
                            if len(raw) != int(lengths[0]):
                                raise ValueError("Truncated room edit request")
                            body = json.loads(raw)
                            if isinstance(body, dict) and body.get("operation") == "rename":
                                body["name"] = normalize_room_name(body.get("name"))
                            document = request_command(
                                command_socket, robot_id, "room_edit", edit=body
                            )
                    except (ValueError, RecursionError) as exc:
                        log("room_edit_rejected", error_type=type(exc).__name__)
                        self.dashboard_reply(
                            json.dumps({"error": str(exc)}).encode(), "application/json", status=400
                        )
                        return
                    except (CommandError, OSError, sqlite3.Error) as exc:
                        log("room_edit_failed", error_type=type(exc).__name__)
                        self.dashboard_reply(
                            json.dumps(
                                {
                                    "error": str(exc)
                                    if isinstance(exc, CommandError)
                                    else "Room edit service unavailable"
                                }
                            ).encode(),
                            "application/json",
                            status=503,
                        )
                        return
                    self.dashboard_reply(
                        json.dumps(document, allow_nan=False).encode(), "application/json"
                    )
                    return
                if editor_route:
                    # Archive availability controls geometry, never authoritative deletion.
                    if self.command != "GET":
                        self.reply(405)
                        return
                    db_path = getattr(self.server, "customer_shadow_db", None)
                    uploads = getattr(self.server, "uploads", None)
                    if db_path is None or uploads is None:
                        self.reply(503)
                        return
                    from roomba_cloud.maps.room_edits import editor_maps

                    try:
                        document = editor_maps(db_path, uploads.path, editor_route[1])
                    except (
                        OSError,
                        sqlite3.Error,
                        ValueError,
                        KeyError,
                        tarfile.TarError,
                        zlib.error,
                        ImportError,
                    ) as exc:
                        log("room_editor_failed", error_type=type(exc).__name__)
                        self.reply(500)
                        return
                    self.dashboard_reply(
                        json.dumps(document, allow_nan=False).encode(), "application/json"
                    )
                    return
                setting_route = re.fullmatch(
                    r"/v1/customer/robots/([0-9A-Fa-f]{32})/settings/([a-z_]{1,64})", path
                )
                command_route = re.fullmatch(
                    r"/v1/customer/robots/([0-9A-Fa-f]{32})/commands/(start|pause|stop|dock|train|evac|drypad|stoppaddry|washpad|clean_segments|live_map)",
                    path,
                )
                if locate or command_route or setting_route:
                    robot_id = (locate or command_route or setting_route)[1]
                    command = (
                        "set_setting" if setting_route else "find" if locate else command_route[2]
                    )
                    if self.command != "POST":
                        self.reply(405)
                        return
                    lengths = self.headers.get_all("Content-Length", [])
                    segment_ids = None
                    setting = None
                    operating_mode = None
                    if command in ("clean_segments", "set_setting") or (
                        command == "start" and lengths not in ([], ["0"])
                    ):
                        if (
                            self.headers.get("Transfer-Encoding")
                            or len(lengths) != 1
                            or not re.fullmatch(r"[0-9]{1,5}", lengths[0])
                            or not 1 <= int(lengths[0]) <= 65536
                        ):
                            self.reply(400)
                            return
                        try:
                            raw = self.rfile.read(int(lengths[0]))
                            if len(raw) != int(lengths[0]):
                                raise ValueError("Truncated room request")
                            body = json.loads(raw)
                            if not isinstance(body, dict):
                                raise ValueError("Invalid command request")
                            expected = (
                                {"value"}
                                if setting_route
                                else {"segment_ids"}
                                if command == "clean_segments"
                                else set()
                            )
                            if not setting_route and "operating_mode" in body:
                                expected = expected | {"operating_mode"}
                                operating_mode = body["operating_mode"]
                                if type(operating_mode) is not int or operating_mode not in (
                                    2,
                                    4,
                                    6,
                                ):
                                    raise ValueError("Invalid cleaning mode")
                            if not isinstance(body, dict) or set(body) != expected:
                                raise ValueError("Invalid room request")
                            if setting_route:
                                from roomba_cloud.robot_settings import SETTINGS

                                if setting_route[2] not in SETTINGS or type(body["value"]) not in (
                                    bool,
                                    int,
                                ):
                                    raise ValueError("Invalid setting request")
                                setting = {"key": setting_route[2], "value": body["value"]}
                            elif command == "clean_segments":
                                segment_ids = body["segment_ids"]
                            if command == "clean_segments" and (
                                not isinstance(segment_ids, list)
                                or not 1 <= len(segment_ids) <= 100
                                or any(
                                    not isinstance(item, str) or len(item) > 300
                                    for item in segment_ids
                                )
                                or len(set(segment_ids)) != len(segment_ids)
                            ):
                                raise ValueError("Invalid segment selection")
                        except (ValueError, RecursionError) as exc:
                            log(
                                "robot_configuration_command_rejected",
                                error_type=type(exc).__name__,
                            )
                            self.reply(400)
                            return
                    elif self.headers.get("Transfer-Encoding") or lengths not in ([], ["0"]):
                        self.reply(400)
                        return
                    command_socket = getattr(self.server, "command_socket", None)
                    if command_socket is None:
                        self.reply(503)
                        return
                    if setting_route:
                        db_path = getattr(self.server, "customer_shadow_db", None)
                        if db_path is None:
                            self.reply(503)
                            return
                        from roomba_cloud.robot_settings import setting_patch

                        try:
                            setting_patch(db_path, robot_id, setting["key"], setting["value"])
                        except ValueError as exc:
                            log("robot_setting_rejected", error_type=type(exc).__name__)
                            self.reply(400)
                            return
                        except (OSError, sqlite3.Error) as exc:
                            log("robot_setting_failed", error_type=type(exc).__name__)
                            self.reply(503)
                            return
                    from roomba_cloud.robot_commands import CommandError, request_command

                    try:
                        result = (
                            request_command(command_socket, robot_id, command, duration=60)
                            if command == "live_map"
                            else request_command(
                                command_socket,
                                robot_id,
                                command,
                                segment_ids,
                                operating_mode=operating_mode,
                            )
                            if operating_mode is not None
                            else request_command(command_socket, robot_id, command, setting=setting)
                            if setting_route
                            else request_command(command_socket, robot_id, command, segment_ids)
                            if command == "clean_segments"
                            else request_command(command_socket, robot_id)
                            if locate
                            else request_command(command_socket, robot_id, command)
                        )
                    except (CommandError, OSError, ValueError) as exc:
                        log("robot_command_failed", error_type=type(exc).__name__)
                        self.reply(503)
                        return
                    payload = json.dumps(result).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(payload)
                    self.close_connection = True
                    log(
                        "robot_command_delivery_acknowledged",
                        command=command,
                        execution="unconfirmed",
                    )
                    return
                if path == "/v1/customer/session":
                    if self.command != "GET":
                        self.reply(405)
                        return
                    payload = b'{"authenticated":true}'
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(payload)
                    self.close_connection = True
                    return
                image_match = re.fullmatch(
                    r"/v1/customer/robots/([0-9A-Fa-f]{32})/maps/"
                    r"([A-Za-z0-9_.-]{1,128})/([A-Za-z0-9_.-]{1,128})/"
                    r"([a-f0-9]{64})/(room|live)\.png",
                    path,
                )
                if image_match:
                    if self.command != "GET":
                        self.reply(405)
                        return
                    db_path = getattr(self.server, "customer_shadow_db", None)
                    if db_path is None:
                        self.reply(503)
                        return
                    from roomba_cloud.maps.customer_maps import MapImageError, image

                    try:
                        uploads = getattr(self.server, "uploads", None)
                        payload = image(
                            db_path,
                            uploads.path if uploads is not None else None,
                            *image_match.groups(),
                        )
                    except (
                        OSError,
                        sqlite3.Error,
                        ValueError,
                        KeyError,
                        tarfile.TarError,
                        zlib.error,
                        ImportError,
                    ) as exc:
                        log(
                            "customer_map_failed",
                            error_type=type(exc).__name__,
                            stage=exc.stage
                            if isinstance(exc, MapImageError)
                            else "archive_catalog",
                            cause_type=type(exc.__cause__).__name__
                            if exc.__cause__
                            else type(exc).__name__,
                        )
                        self.reply(500)
                        return
                    if payload is None:
                        self.reply(404)
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Content-Length", str(len(payload)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(payload)
                    self.close_connection = True
                    return
                match = re.fullmatch(
                    r"/v1/customer/robots/([0-9A-Fa-f]{32})/(telemetry|events)", path
                )
                if match:
                    if self.command != "GET":
                        self.reply(405)
                        return
                    db_path = getattr(self.server, "customer_shadow_db", None)
                    if db_path is None:
                        log("customer_telemetry_failed", reason="shadow API disabled")
                        self.reply(503)
                        return
                    if match[2] == "events":
                        self.telemetry_stream(db_path, match[1])
                        return
                    from roomba_cloud.customer_telemetry import snapshot

                    try:
                        uploads = getattr(self.server, "uploads", None)
                        document = snapshot(
                            db_path,
                            match[1],
                            uploads.path if uploads is not None else None,
                            getattr(self.server, "customer_acme_challenge_file", None),
                        )
                        payload = json.dumps(document, allow_nan=False).encode()
                    except (
                        OSError,
                        sqlite3.Error,
                        ValueError,
                        TypeError,
                        KeyError,
                        tarfile.TarError,
                        zlib.error,
                    ) as exc:
                        log("customer_telemetry_failed", error_type=type(exc).__name__)
                        self.reply(500)
                        return
                    if document is None:
                        self.reply(404)
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(payload)
                    self.close_connection = True
                    return
            lengths = self.headers.get_all("Content-Length", [])
            if (
                self.headers.get("Transfer-Encoding")
                or len(lengths) > 1
                or (lengths and not re.fullmatch(r"[0-9]{1,6}", lengths[0]))
            ):
                log("http_request_rejected", reason="invalid body framing")
                self.reply(400)
                return
            length = int(lengths[0]) if lengths else 0
            if length > 65536:
                log("http_request_rejected", reason="body exceeds diagnostic limit")
                self.reply(413)
                return
            body = self.rfile.read(length)
            if len(body) != length:
                log("http_request_rejected", reason="truncated body")
                self.reply(400)
                return
            headers = {
                key.lower(): "<redacted>"
                if key.lower() in REDACT and (not SHOW_SECRETS or auth is not None)
                else value
                for key, value in self.headers.items()
            }
            if auth is not None and not robot_route(path):
                log(
                    "customer_request",
                    peer=self.client_address[0],
                    method=self.command,
                    body_bytes=len(body),
                )
            else:
                log(
                    "http_request",
                    peer=self.client_address[0],
                    request=self.requestline,
                    headers=headers,
                    body_bytes=len(body),
                    **logged_body(body),
                )
            status, response = http_response(
                self.requestline, self.server.discovery, self.server.registration_ok
            )
            registration = re.fullmatch(r"/v1/([0-9A-Fa-f]{32})/register", path)
            db_path = getattr(self.server, "customer_shadow_db", None)
            if (
                registration
                and self.command == "POST"
                and status == "200 OK"
                and db_path is not None
            ):
                from roomba_cloud.shadow_service import ShadowService

                try:
                    sku = None
                    if body:
                        try:
                            document = json.loads(body)
                        except (ValueError, UnicodeDecodeError, RecursionError):
                            document = None
                        if isinstance(document, dict):
                            value = document.get("sku")
                            if isinstance(value, str) and 0 < len(value) <= 128:
                                sku = value
                    ShadowService(db_path).record_registration(
                        registration[1], self.client_address[0], sku
                    )
                except (OSError, sqlite3.Error, ValueError) as exc:
                    log("robot_inventory_failed", error_type=type(exc).__name__)
                    self.reply(500)
                    return
            self.connection.sendall(response)
            self.close_connection = True
            log("http_response", peer=self.client_address[0], status=status)

        def dashboard_reply(self, payload, content_type, status=200):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            )
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
            self.close_connection = True

        def telemetry_stream(self, db_path, robot_id):
            """Stream changed full snapshots, reconciling every 15 seconds.

            Subscribe before the initial read to avoid a commit gap. Notifications
            are advisory/coalesced: heartbeats re-read durable state for expiry,
            missed datagrams and process crashes. Once headers are sent, a failure
            closes/logs the stream; it cannot send a second HTTP status response.
            """
            from roomba_cloud.customer_telemetry import snapshot

            events = getattr(self.server, "telemetry_events", None)
            if events is None or events.stopped:
                self.reply(503)
                return
            self.close_connection = True
            started = False
            try:
                # Subscribe before the initial read so commits cannot fall into a gap.
                with events.subscribe(robot_id) as changed:
                    uploads = getattr(self.server, "uploads", None)
                    map_db = uploads.path if uploads is not None else None
                    challenge_file = getattr(self.server, "customer_acme_challenge_file", None)
                    document = snapshot(db_path, robot_id, map_db, challenge_file)
                    if document is None:
                        self.reply(404)
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Connection", "close")
                    self.send_header("X-Accel-Buffering", "no")
                    self.end_headers()
                    started = True
                    previous = None
                    while not events.stopped:
                        payload = json.dumps(
                            document, allow_nan=False, separators=(",", ":")
                        ).encode()
                        if payload != previous:
                            self.wfile.write(b"event: telemetry\ndata: " + payload + b"\n\n")
                            previous = payload
                        else:
                            self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
                        changed.wait(15)
                        changed.clear()
                        if events.stopped:
                            break
                        # Also reconcile lease expiry, process crashes and lost wakeups.
                        document = snapshot(db_path, robot_id, map_db, challenge_file)
                        if document is None:
                            raise ValueError("Telemetry disappeared during stream")
            except (
                OSError,
                sqlite3.Error,
                ValueError,
                TypeError,
                KeyError,
                tarfile.TarError,
                zlib.error,
            ) as exc:
                log("customer_stream_closed", error_type=type(exc).__name__)
                if not started:
                    self.reply(500)

    return CloudHTTPHandler


def cloud_server():
    from roomba_cloud.maps.upload import MapUploadServer

    return MapUploadServer


def main():
    """Start HTTPS with explicit optional services and one owned telemetry listener.

    Validate private files before binding. Shared listener TLS >=1.2 uses a
    permissive cipher compatibility configuration for the tested V4 firmware;
    this does not disable the robot/client's server-chain verification. See
    MapUploadServer for structured pre-HTTP handshake diagnostics.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=443)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--cert", required=True, help="trusted PEM certificate chain")
    parser.add_argument("--key", required=True, help="PEM private key")
    parser.add_argument(
        "--discovery-file",
        default=str(Path(__file__).with_name("discovery_response.json")),
        help="robot discovery JSON (default: bundled discovery_response.json)",
    )
    parser.add_argument(
        "--map-upload-db",
        type=Path,
        help="enable private map PUT uploads on this listener using the shared MQTT upload database",
    )
    parser.add_argument("--map-max-bytes", type=int, default=64 * 1024 * 1024)
    parser.add_argument("--map-max-total-bytes", type=int, default=256 * 1024 * 1024)
    parser.add_argument(
        "--customer-token-file",
        type=Path,
        help="private file containing a pre-shared bearer token; "
        "protect customer routes, not robot discovery/registration/uploads",
    )
    parser.add_argument(
        "--customer-shadow-db",
        type=Path,
        help="enable sanitized read-only customer telemetry from this existing shadow database",
    )
    parser.add_argument(
        "--customer-acme-challenge-file",
        type=Path,
        help="public DNS challenge JSON to include in authenticated customer telemetry",
    )
    parser.add_argument(
        "--command-socket",
        type=Path,
        help="private Unix socket of the MQTT service; enables HA vacuum commands",
    )
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    try:
        customer_auth = (
            CustomerAuth.load(args.customer_token_file) if args.customer_token_file else None
        )
    except (OSError, ValueError) as exc:
        parser.error(f"Cannot load customer token: {exc}")
    if args.command_socket and customer_auth is None:
        parser.error("--command-socket requires --customer-token-file")
    if args.customer_shadow_db:
        if customer_auth is None:
            parser.error("--customer-shadow-db requires --customer-token-file")
        try:
            info = args.customer_shadow_db.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise ValueError("Shadow database must be a private regular file")
            with contextlib.closing(
                sqlite3.connect(args.customer_shadow_db.resolve().as_uri() + "?mode=ro", uri=True)
            ) as db:
                db.execute("SELECT topic, document FROM shadows LIMIT 1")
        except (OSError, ValueError, sqlite3.Error) as exc:
            parser.error(f"Cannot open customer shadow database: {type(exc).__name__}")
    if args.map_upload_db:
        if not 1 <= args.map_max_bytes <= args.map_max_total_bytes <= 1024**3:
            parser.error("Invalid map upload limits (maximum total 1 GiB)")
    try:
        discovery = load_discovery(args.discovery_file)
    except (OSError, ValueError) as exc:
        parser.error(f"Cannot load discovery response: {exc}")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    # V4 compatibility policy; keep distinct from customer token authentication.
    context.set_ciphers("ALL:@SECLEVEL=0")
    context.load_cert_chain(args.cert, args.key)
    from roomba_cloud.maps.upload import MapUploadService
    from roomba_cloud.telemetry_events import TelemetryEvents

    uploads = MapUploadService(args.map_upload_db) if args.map_upload_db else None
    if args.customer_shadow_db:
        from roomba_cloud.maps.room_edits import initialize

        initialize(args.customer_shadow_db)
    with cloud_server()((args.bind, args.port), cloud_handler()) as server:
        server.customer_auth = customer_auth
        server.command_socket = args.command_socket
        server.customer_shadow_db = args.customer_shadow_db
        server.customer_acme_challenge_file = args.customer_acme_challenge_file
        server.uploads = uploads
        server.max_bytes = args.map_max_bytes
        server.max_total = args.map_max_total_bytes
        server.tls_context = context
        server.discovery = discovery
        server.registration_ok = True
        with contextlib.ExitStack() as resources:
            server.telemetry_events = (
                resources.enter_context(TelemetryEvents(args.customer_shadow_db))
                if args.customer_shadow_db
                else None
            )
            log(
                "cloud_http_listening",
                bind=args.bind,
                port=server.server_port,
                registration_ok=True,
                map_uploads=uploads is not None,
                customer_auth=customer_auth is not None,
                telemetry_push=server.telemetry_events is not None,
                max_bytes=args.map_max_bytes,
                max_total_bytes=args.map_max_total_bytes,
            )
            server.serve_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Stopped.", file=sys.stderr)
    except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError) as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
