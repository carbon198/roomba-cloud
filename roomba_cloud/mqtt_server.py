#!/usr/bin/env python3
"""The robot-facing MQTT daemon, retaining its historical diagnostic filename.

Use python -m roomba_cloud for deployments. This is a small MQTT 3.1/3.1.1/5 endpoint for
the tested robot's shadows, uploads and one-shot commands, not a general broker.
No upstream cloud forwarding, durable command queue or command replay exists.
The client ID identifies a robot without enrollment or source-IP assumptions;
CONNECT password fields are counted/redacted, NOT authenticated. LAN firewall
is the robot-side trust boundary. Customer HTTPS uses separate bearer auth.

The native mqtt_tls_bridge supplies the TLS 1.2 client-certificate request needed
by the tested 505. Its certificate acceptance is compatibility, not verification
of robot identity. See ClientCertificateTransport and the C source for pipe
framing; never interpret its first JSON line as an MQTT packet.

Cloud map research not implemented as a responder:
``v011-irbthbu/things/<id>/mapdetails/req`` carries request_id and p2map_ids.
Broker registration 0x4b24a0, event 0x139 routing 0x4a5470, Matter handling
0x47b0d0 and forwarding 0x47917c lead to chip-irobot-app. Matter recognition
0xb1108, map parsing 0x72acc, restoration 0x73658 and room parsing 0x74508
suggest an envelope echoing request_id with a maps array containing p2map_id,
p2mapv_id, optional name and regions (region_id, region_type="rid", optional
name). This is metadata restoration, NOT map geometry download. No end-to-end
response has been validated, so requests are only captured. A future responder
must use complete, robot-scoped, version-matched archives: Matter clears and
rebuilds supported maps/areas on inventory changes, so a partial/empty success
for unavailable archives could erase supported areas. Fail explicitly instead.
Firmware hashes and other address provenance are recorded in room_edits.
"""

import argparse
import ipaddress
import base64
import contextlib
import json
import os
from pathlib import Path
import select
import socket
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import threading
import time

from roomba_cloud.robot_mqtt import ProtocolError, extract_packet, mqtt_string, packet
from roomba_cloud.shadow_service import ShadowService
from roomba_cloud.maps.upload import MapUploadService
from roomba_cloud.cloud_http import log, parse_client_hello


class PrivateCapture:
    """Opt-in bounded private evidence, not ordinary service logging.

    Store only allowlisted telemetry/request topics as base64 JSONL, never
    credential fields from CONNECT. Exclusive creation and a size cap prevent
    silent overwrite/unbounded recording. Maps, room names and pose data remain
    sensitive even without credentials; do not publish captures in source/issues.
    """

    def __init__(self, path, max_bytes=8 * 1024 * 1024):
        if path.parent.stat().st_mode & 0o077:
            raise ValueError(
                "Capture parent directory must not allow group/other access (use chmod 700)"
            )
        self.lock = threading.Lock()
        self.max_bytes = max_bytes
        self.bytes_written = 0
        self.failed = False
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self.output = os.fdopen(fd, "wb")

    def record(self, peer, client_id, fields, payload):
        shadow = f"$aws/things/{client_id}/shadow/name/"
        custom = f"v011-irbthbu/things/{client_id}/"
        topics = {
            f"$aws/things/{client_id}/shadow/get",
            f"$aws/things/{client_id}/shadow/update",
            custom + "mapdetails/req",
            custom + "filexfer_req",
            custom + "livemap",
            custom + "mission/timeline",
        }
        topics.update(
            shadow + name + "/" + action
            for name in (
                "ro-currentstate",
                "na-irbtfeatures",
                "ro-configinfo",
                "ro-services",
                "ro-stats",
                "rw-constatus",
                "rw-schedule",
                "rw-settings",
                "rw-software",
            )
            for action in ("get", "update")
        )
        topic = fields["topic"]
        data_topic = False
        for family in ("livemap/", "mission/timeline/"):
            prefix = custom + family
            if topic.startswith(prefix):
                suffix = topic[len(prefix) :]
                data_topic = bool(suffix) and not any(
                    part in ("cmd", "request") for part in suffix.split("/")
                )
                break
        if topic not in topics and not data_topic:
            return False
        record = {
            "t": round(time.time(), 3),
            "peer": peer,
            "client_id": client_id,
            **fields,
            "payload_base64": base64.b64encode(payload).decode("ascii"),
        }
        data = (json.dumps(record) + "\n").encode("utf-8")
        with self.lock:
            if self.failed or self.output.closed:
                raise ProtocolError("Private capture unavailable; restart with a new output file")
            if self.bytes_written + len(data) > self.max_bytes:
                self.failed = True
                raise ProtocolError(
                    "Private capture size limit reached; restart with a new output file"
                )
            try:
                self.output.write(data)
                self.output.flush()
                os.fsync(self.output.fileno())
            except OSError:
                self.failed = True
                raise
            self.bytes_written += len(data)
        return True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        with self.lock:
            self.output.close()


def connect_metadata(header, body):
    """Parse enough CONNECT to negotiate framing without retaining credential bytes."""
    if header != 0x10:
        raise ProtocolError("Expected MQTT CONNECT as the first packet")
    pos = 0

    def take(size):
        nonlocal pos
        if pos + size > len(body):
            raise ProtocolError("Truncated MQTT CONNECT")
        value = body[pos : pos + size]
        pos += size
        return value

    def blob():
        return take(int.from_bytes(take(2), "big"))

    def skip_properties():
        length = 0
        for index in range(4):
            digit = take(1)[0]
            length += (digit & 127) * (128**index)
            if not digit & 128:
                take(length)
                return
        raise ProtocolError("Malformed MQTT property length")

    protocol = blob()
    version, flags = take(2)
    if (protocol, version) not in ((b"MQIsdp", 3), (b"MQTT", 4), (b"MQTT", 5)):
        raise ProtocolError("Unsupported MQTT protocol name/version")
    will = bool(flags & 4)
    will_qos = (flags >> 3) & 3
    if flags & 1 or will_qos == 3 or (not will and flags & 0x38):
        raise ProtocolError("Invalid MQTT CONNECT flags")
    if version != 5 and flags & 0x40 and not flags & 0x80:
        raise ProtocolError("MQTT 3.x password flag requires username flag")
    keepalive = int.from_bytes(take(2), "big")
    if version == 5:
        skip_properties()
    client_id = blob()
    try:
        client_id = client_id.decode("utf-8")
    except UnicodeDecodeError:
        raise ProtocolError("Invalid MQTT client ID encoding") from None
    if "\0" in client_id:
        raise ProtocolError("Invalid MQTT client ID")
    will_topic_bytes = will_payload_bytes = 0
    if will:
        if version == 5:
            skip_properties()
        will_topic_bytes = len(blob())
        will_payload_bytes = len(blob())
    username_bytes = len(blob()) if flags & 0x80 else 0
    password_bytes = len(blob()) if flags & 0x40 else 0
    if pos != len(body):
        raise ProtocolError("Trailing bytes in MQTT CONNECT")
    return {
        "protocol_version": version,
        "client_id": client_id,
        "clean_session_or_start": bool(flags & 2),
        "keepalive_seconds": keepalive,
        "username_present": bool(flags & 0x80),
        "username_bytes": username_bytes,
        "password_present": bool(flags & 0x40),
        "password_bytes": password_bytes,
        "will_present": will,
        "will_qos": will_qos,
        "will_topic_bytes": will_topic_bytes,
        "will_payload_bytes": will_payload_bytes,
    }


def read_frame(conn, timeout, buffer):
    """Use one deadline per packet while retaining partial/coalesced frames in buffer."""
    deadline = time.monotonic() + timeout
    while True:
        frame = extract_packet(buffer)
        if frame is not None:
            return frame
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Timed out waiting for MQTT packet")
        conn.settimeout(remaining)
        chunk = conn.recv(8192)
        if not chunk:
            raise ConnectionError("Client closed before a complete MQTT packet")
        buffer.extend(chunk)


def read_connect(conn, timeout, buffer=None):
    return connect_metadata(*read_frame(conn, timeout, bytearray() if buffer is None else buffer))


def unavailable_connack(version):
    return packet(0x20, b"\0\x88\0" if version == 5 else b"\0\x03")


def successful_connack(version):
    # MQTT 5: advertise maximum QoS 1, no retained messages, no topic aliases.
    return packet(0x20, b"\0\0\x07\x24\x01\x25\0\x22\0\0" if version == 5 else b"\0\0")


class PacketReader:
    """Bounds-checked MQTT body cursor; never consume fields past the current frame."""

    def __init__(self, body):
        self.body = body
        self.pos = 0

    def take(self, size):
        if self.pos + size > len(self.body):
            raise ProtocolError("Truncated MQTT packet")
        result = self.body[self.pos : self.pos + size]
        self.pos += size
        return result

    def identifier(self):
        value = int.from_bytes(self.take(2), "big")
        if not value:
            raise ProtocolError("Zero MQTT packet identifier")
        return value

    def text(self):
        raw = self.take(int.from_bytes(self.take(2), "big"))
        try:
            value = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ProtocolError("Invalid MQTT topic encoding") from None
        if not value or any(
            ord(c) < 32
            or 0x7F <= ord(c) <= 0x9F
            or 0xD800 <= ord(c) <= 0xDFFF
            or ord(c) & 0xFFFF in (0xFFFE, 0xFFFF)
            for c in value
        ):
            raise ProtocolError("Invalid MQTT topic")
        return value

    def properties(self):
        length = 0
        for index in range(4):
            digit = self.take(1)[0]
            length += (digit & 127) * 128**index
            if not digit & 128:
                self.take(length)
                return
        raise ProtocolError("Malformed MQTT properties")

    def end(self):
        if self.pos != len(self.body):
            raise ProtocolError("Trailing MQTT packet bytes")


def observation_packet(header, body, version):
    """Validate supported packet flags and return event, metadata, reply, close flag.

    QoS is limited to 0/1. MQTT 5 properties are bounded/skipped, not treated as
    a promise of broker features such as topic aliases or session persistence.
    Publication payload bytes remain separate from normal log metadata.
    """
    reader = PacketReader(body)
    if header in (0x82, 0xA2):
        identifier = reader.identifier()
        if version == 5:
            reader.properties()
        topics, reasons = [], []
        while reader.pos < len(body):
            topic = reader.text()
            if any(
                ("+" in level and level != "+")
                or ("#" in level and (level != "#" or index != len(topic.split("/")) - 1))
                for index, level in enumerate(topic.split("/"))
            ):
                raise ProtocolError("Invalid MQTT topic filter")
            if header == 0x82:
                options = reader.take(1)[0]
                if options & 0xC0 or options & 3 == 3 or (options >> 4) & 3 == 3:
                    raise ProtocolError("Invalid subscription options")
                if version != 5 and options & ~3:
                    raise ProtocolError("Invalid MQTT 3 subscription options")
                if topic.startswith("$share/"):
                    parts = topic.split("/", 2)
                    if (
                        len(parts) != 3
                        or not parts[1]
                        or not parts[2]
                        or "+" in parts[1]
                        or "#" in parts[1]
                    ):
                        raise ProtocolError("Invalid shared subscription")
                    if options & 4:
                        raise ProtocolError("No Local on shared subscription")
                qos = min(options & 3, 1)
                topics.append({"topic": topic, "requested_qos": options & 3, "granted_qos": qos})
                reasons.append(qos)
            else:
                topics.append({"topic": topic})
                reasons.append(0)
        if not topics:
            raise ProtocolError("Empty MQTT subscription packet")
        response = identifier.to_bytes(2, "big")
        if version == 5:
            response += b"\0"
        if header == 0x82 or version == 5:
            response += bytes(reasons)
        return (
            "mqtt_subscribe" if header == 0x82 else "mqtt_unsubscribe",
            {"packet_id": identifier, "topics": topics},
            packet(0x90 if header == 0x82 else 0xB0, response),
            False,
        )
    if header >> 4 == 3:
        qos = (header >> 1) & 3
        if qos > 1 or (qos == 0 and header & 8):
            raise ProtocolError("Unsupported MQTT publication flags/QoS")
        topic = reader.text()
        if "+" in topic or "#" in topic:
            raise ProtocolError("Wildcard in publication topic")
        identifier = reader.identifier() if qos else None
        if version == 5:
            reader.properties()
        return (
            "mqtt_publish",
            {
                "topic": topic,
                "qos": qos,
                "retain": bool(header & 1),
                "duplicate": bool(header & 8),
                "packet_id": identifier,
                "payload_bytes": len(body) - reader.pos,
            },
            packet(0x40, identifier.to_bytes(2, "big")) if qos else None,
            False,
        )
    if header == 0xC0:
        reader.end()
        return "mqtt_ping", {}, packet(0xD0), False
    if header == 0x40:
        identifier = reader.identifier()
        reason = 0
        if version == 5 and reader.pos < len(body):
            reason = reader.take(1)[0]
            if reader.pos < len(body):
                reader.properties()
        reader.end()
        return "mqtt_puback", {"packet_id": identifier, "reason_code": reason}, None, False
    if header == 0xE0:
        reason = 0
        if version == 5 and body:
            reason = reader.take(1)[0]
            if reader.pos < len(body):
                reader.properties()
        reader.end()
        return "mqtt_disconnect", {"reason_code": reason}, None, True
    raise ProtocolError(f"Unsupported MQTT packet type/flags 0x{header:02x}")


def topic_matches(subscription, topic):
    """Match MQTT wildcards, including the special nonmatching '$' root rule."""
    levels = topic.split("/")
    filters = subscription.split("/")
    if topic.startswith("$") and filters[0] in ("+", "#"):
        return False
    for index, part in enumerate(filters):
        if part == "#":
            return True
        if index >= len(levels) or part not in ("+", levels[index]):
            return False
    return len(filters) == len(levels)


class SessionMaintenance:
    """Coalesce session activity off all MQTT readers in one storage worker.

    Packet receipt times, not delayed flush times, determine freshness. Disconnect
    cleanup runs after any in-flight renewal; session IDs isolate reconnects.
    Dirty sessions flush every five seconds; failures are logged and retried.
    Storage stalls can still expire persisted leases. Startup registration and
    application persistence remain synchronous; shutdown waits at most one second.
    """

    interval = 5
    join_timeout = 1

    def __init__(self, shadows):
        self.shadows = shadows
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.stopping = False
        self.pending = {}
        self.ended = set()
        self.worker = threading.Thread(target=self._run, daemon=True)

    def record(self, session, lease, data_received=False):
        now = time.time()
        with self.lock:
            previous = self.pending.get(session)
            data_at = previous[2] if previous else None
            if data_received:
                data_at = now
            self.pending[session] = (lease, now, data_at)

    def end(self, session):
        with self.lock:
            self.pending.pop(session, None)
            self.ended.add(session)
        self.wake.set()

    def _flush(self):
        with self.lock:
            pending, self.pending = self.pending, {}
            ended, self.ended = self.ended, set()
        for session, (lease, observed_at, data_at) in pending.items():
            try:
                self.shadows.touch_session(
                    session, lease, observed_at=observed_at, data_received_at=data_at
                )
            except (OSError, sqlite3.Error, ValueError) as exc:
                log("mqtt_session_maintenance_failed", operation="touch", error=str(exc))
                with self.lock:
                    if session not in self.ended and session not in ended:
                        current = self.pending.get(session)
                        if current is None:
                            self.pending[session] = (lease, observed_at, data_at)
                        elif data_at is not None:
                            self.pending[session] = (
                                current[0],
                                current[1],
                                max(data_at, current[2]) if current[2] is not None else data_at,
                            )
        for session in ended:
            try:
                self.shadows.end_session(session)
            except (OSError, sqlite3.Error, ValueError) as exc:
                log("mqtt_session_maintenance_failed", operation="end", error=str(exc))
                with self.lock:
                    self.ended.add(session)

    def _run(self):
        while True:
            self.wake.wait(self.interval)
            self.wake.clear()
            self._flush()
            with self.lock:
                if self.stopping:
                    break
        # An end can arrive while the final renewal is blocked in storage.
        self._flush()

    def __enter__(self):
        self.worker.start()
        return self

    def __exit__(self, *args):
        with self.lock:
            self.stopping = True
        self.wake.set()
        self.worker.join(self.join_timeout)
        if self.worker.is_alive():
            log("mqtt_session_maintenance_stopping", message="Storage operation still pending")


def observe(
    conn,
    peer,
    metadata,
    buffer,
    duration,
    capture=None,
    shadows=None,
    map_uploads=None,
    map_response_delay_ms=0,
    session=None,
    command_session=None,
    maintenance=None,
):
    """Pump one connection; transport ACK precedes application response work.

    Only deliver shadow/upload replies to subscribed topics. A fresh accepted
    current-state update containing p2maps, plus an edit-topic subscription,
    gates bootstrap: do not act on stale disk inventory at CONNECT time.
    Missing-map discovery runs once per connection; later inventory reports can
    probe edit-locked maps. A background pass is needed because publish_once
    waits for PUBACKs that this reader must continue processing.
    """
    deadline = time.monotonic() + duration if duration is not None else float("inf")
    idle = max(10, metadata["keepalive_seconds"] * 1.5)
    subscriptions = set()
    if command_session is not None:
        conn = command_session
        subscriptions = command_session.subscriptions
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            log("mqtt_observation_complete", peer=peer, reason="duration limit")
            return
        try:
            header, body = read_frame(conn, min(idle, remaining), buffer)
        except TimeoutError:
            if time.monotonic() >= deadline:
                log("mqtt_observation_complete", peer=peer, reason="duration limit")
                return
            raise
        event, fields, response, done = observation_packet(
            header, body, metadata["protocol_version"]
        )
        if session is not None:
            maintenance.record(session, idle + 20, data_received=event == "mqtt_publish")
        log(event, peer=peer, **fields)
        if event == "mqtt_puback" and command_session is not None:
            command_session.acknowledge(fields["packet_id"], fields["reason_code"])
        if (
            event == "mqtt_publish"
            and command_session is not None
            and shadows is not None
            and fields["topic"] == f"v011-irbthbu/things/{metadata['client_id']}/edit_resp"
        ):
            from roomba_cloud.maps.room_edits import receive_result

            try:
                receive_result(
                    shadows.path, metadata["client_id"], body[len(body) - fields["payload_bytes"] :]
                )
            except (ValueError, TypeError, RecursionError) as exc:
                log("room_edit_response_rejected", error_type=type(exc).__name__)
        if capture is not None and event == "mqtt_publish":
            payload = body[len(body) - fields["payload_bytes"] :]
            if capture.record(peer, metadata["client_id"], fields, payload):
                log(
                    "mqtt_payload_saved",
                    peer=peer,
                    topic=fields["topic"],
                    payload_bytes=fields["payload_bytes"],
                )
        if response is not None:
            conn.sendall(response)
        if event == "mqtt_subscribe":
            subscriptions.update(item["topic"] for item in fields["topics"])
        elif event == "mqtt_unsubscribe":
            subscriptions.difference_update(item["topic"] for item in fields["topics"])
        elif event == "mqtt_publish" and shadows is not None:
            payload = body[len(body) - fields["payload_bytes"] :]
            try:
                reply = shadows.respond(fields["topic"], payload, metadata["client_id"])
            except (ValueError, TypeError, RecursionError) as exc:
                log("mqtt_live_telemetry_rejected", error_type=type(exc).__name__)
                reply = None
            if (
                command_session is not None
                and map_uploads is not None
                and not command_session.map_refresh_in_progress
                and fields["topic"]
                == f"$aws/things/{metadata['client_id']}/shadow/name/ro-currentstate/update"
                and reply is not None
                and "code" not in reply[1]
                and any(topic_matches(s, command_session.edit_topic) for s in subscriptions)
            ):
                reported = json.loads(payload).get("state", {}).get("reported", {})
                if isinstance(reported, dict) and "p2maps" in reported:
                    from roomba_cloud.maps.room_edits import bootstrap_once

                    include_missing = not command_session.map_bootstrap_attempted
                    command_session.map_bootstrap_attempted = True
                    command_session.map_refresh_in_progress = True
                    threading.Thread(
                        target=bootstrap_once,
                        args=(
                            shadows.path,
                            map_uploads.path,
                            metadata["client_id"],
                            command_session,
                            include_missing,
                        ),
                        daemon=True,
                    ).start()
            shadows.notify(metadata["client_id"])
            if reply is not None:
                topic, document = reply
                subscribed = any(topic_matches(s, topic) for s in subscriptions)
                if subscribed:
                    encoded = json.dumps(document, ensure_ascii=True, allow_nan=False).encode()
                    wire = mqtt_string(topic) + (
                        b"\0" if metadata["protocol_version"] == 5 else b""
                    )
                    conn.sendall(packet(0x30, wire + encoded))
                log(
                    "mqtt_shadow_response",
                    peer=peer,
                    topic=topic,
                    delivered=subscribed,
                    code=document.get("code", 200),
                    version=document.get("version"),
                    **({"message": document["message"]} if "code" in document else {}),
                )
        if event == "mqtt_publish" and map_uploads is not None:
            topic = f"v011-irbthbu/things/{metadata['client_id']}/filexfer_resp"
            request_topic = topic.rsplit("/", 1)[0] + "/filexfer_req"
            if fields["topic"] == request_topic:
                if not any(topic_matches(s, topic) for s in subscriptions):
                    log("map_upload_not_negotiated", reason="response topic not subscribed")
                else:
                    payload = body[len(body) - fields["payload_bytes"] :]
                    reply = map_uploads.respond(fields["topic"], payload, metadata["client_id"])
                    if reply is not None:
                        if map_response_delay_ms:
                            log("map_upload_response_delayed", delay_ms=map_response_delay_ms)
                            time.sleep(map_response_delay_ms / 1000)
                        response_topic, document = reply
                        encoded = json.dumps(document, ensure_ascii=True, allow_nan=False).encode()
                        wire = mqtt_string(response_topic) + (
                            b"\0" if metadata["protocol_version"] == 5 else b""
                        )
                        conn.sendall(packet(0x30, wire + encoded))
                        log(
                            "map_upload_negotiated",
                            transfer_type=document["reqParams"]["xferType"],
                            response_delay_ms=map_response_delay_ms,
                        )
        if done:
            return


class ClientCertificateTransport:
    """Socket-like facade over one owned OpenSSL bridge subprocess.

    The passed socket carries TLS. Pipe stdout is one bounded JSON handshake line
    followed by plaintext MQTT; stdin is plaintext output. Private stderr is read
    only on failure. Deadlines bound pipe backpressure; exit reaps the child so
    a disconnected robot cannot leave a transport helper behind.
    """

    def __init__(self, raw, cert, key, bridge, timeout):
        self.timeout = timeout
        self.errors = tempfile.TemporaryFile()
        try:
            self.process = subprocess.Popen(
                [str(bridge), str(raw.fileno()), str(cert), str(key), str(timeout)],
                pass_fds=(raw.fileno(),),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self.errors,
                bufsize=0,
            )
        except OSError:
            self.errors.close()
            raise
        os.set_blocking(self.process.stdin.fileno(), False)

    def failure(self):
        self.errors.seek(0)
        detail = self.errors.read(4096).decode("utf-8", errors="replace").strip()
        return ConnectionError(f"OpenSSL transport closed: {detail or 'no TLS diagnostics'}")

    def settimeout(self, timeout):
        self.timeout = timeout

    def recv(self, size):
        if not select.select([self.process.stdout], [], [], self.timeout)[0]:
            raise TimeoutError("Timed out reading OpenSSL transport")
        chunk = os.read(self.process.stdout.fileno(), size)
        if not chunk and self.process.poll() not in (None, 0):
            raise self.failure()
        return chunk

    def handshake(self):
        deadline = time.monotonic() + self.timeout
        line = bytearray()
        while len(line) < 4096:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Timed out waiting for TLS handshake")
            self.settimeout(remaining)
            chunk = self.recv(1)
            if not chunk:
                raise self.failure()
            if chunk == b"\n":
                return json.loads(line)
            line.extend(chunk)
        raise ProtocolError("Oversized TLS handshake metadata")

    def sendall(self, data):
        deadline = time.monotonic() + self.timeout
        while data:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([], [self.process.stdin], [], remaining)[1]:
                raise TimeoutError("Timed out writing OpenSSL transport")
            try:
                count = os.write(self.process.stdin.fileno(), data)
            except BlockingIOError:
                continue
            except BrokenPipeError as exc:
                raise self.failure() from exc
            data = data[count:]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.process.stdout.close()
        self.errors.close()


def handle(
    raw,
    peer,
    context,
    timeout=10,
    client_auth=None,
    observe_seconds=None,
    capture=None,
    shadows=None,
    map_uploads=None,
    map_response_delay_ms=0,
    commands=None,
    maintenance=None,
):
    """Own TLS/MQTT/session lifetime for one peer, releasing its lease on exit.

    The native and Python transports share the packet parser. Replacing a robot
    command session closes the old session; cleanup must remove only the old
    instance, never disconnect a newer connection using the same client ID.
    """
    from roomba_cloud.server import HEALTHCHECK_CLIENT_ID

    def log_handshake():
        log("connection", peer=peer[0], port=peer[1])
        log("client_hello", peer=peer[0], **parse_client_hello(hello))
        log("handshake_ok", peer=peer[0], **handshake)

    try:
        raw.settimeout(timeout)
        hello = None
        try:
            hello = raw.recv(4096, socket.MSG_PEEK)
            if client_auth:
                cert, key, bridge = client_auth
                raw.setblocking(True)
                conn = ClientCertificateTransport(
                    raw,
                    cert,
                    key,
                    bridge,
                    max(timeout, observe_seconds or (3600 if shadows is not None else timeout)),
                )
                conn.settimeout(timeout)
                try:
                    handshake = conn.handshake()
                except (OSError, ProtocolError, ValueError):
                    conn.__exit__(None, None, None)
                    raise
            else:
                conn = context.wrap_socket(raw, server_side=True)
                handshake = {"tls": conn.version(), "cipher": conn.cipher()[0]}
        except (OSError, ProtocolError, ValueError) as exc:
            log("connection", peer=peer[0], port=peer[1])
            if hello is not None:
                log("client_hello", peer=peer[0], **parse_client_hello(hello))
            log("handshake_failed", peer=peer[0], error=str(exc))
            return
        with conn:
            handshake_logged = False
            try:
                buffer = bytearray()
                metadata = read_connect(conn, timeout, buffer)
                if (
                    ipaddress.ip_address(peer[0]).is_loopback
                    and metadata["client_id"] == HEALTHCHECK_CLIENT_ID
                    and metadata["protocol_version"] == 4
                    and metadata["clean_session_or_start"]
                    and not metadata["will_present"]
                    and not metadata["username_present"]
                    and not metadata["password_present"]
                    and (observe_seconds is not None or shadows is not None)
                ):
                    conn.sendall(successful_connack(4))
                    if read_frame(conn, timeout, buffer) != (0xE0, b""):
                        raise ProtocolError(
                            "Healthcheck must disconnect without sending robot data"
                        )
                    return
                log_handshake()
                handshake_logged = True
                log("mqtt_connect", peer=peer[0], **metadata)
                if observe_seconds is not None or shadows is not None:
                    conn.sendall(successful_connack(metadata["protocol_version"]))
                    log(
                        "mqtt_observation_started",
                        peer=peer[0],
                        duration_seconds=observe_seconds,
                        authenticates_identity=False,
                        forwards=False,
                        sends_commands=commands is not None,
                    )
                    session = (
                        shadows.start_session(
                            metadata["client_id"],
                            max(10, metadata["keepalive_seconds"] * 1.5) + 20,
                            peer_ip=peer[0],
                        )
                        if shadows is not None
                        else None
                    )
                    command_session = None
                    if commands is not None:
                        from roomba_cloud.robot_commands import CommandSession

                        command_session = CommandSession(
                            conn, metadata["protocol_version"], metadata["client_id"]
                        )
                        commands.register(metadata["client_id"], command_session)
                    manager = (
                        SessionMaintenance(shadows)
                        if session is not None and maintenance is None
                        else contextlib.nullcontext(maintenance)
                    )
                    with manager as activity:
                        try:
                            observe(
                                conn,
                                peer[0],
                                metadata,
                                buffer,
                                observe_seconds,
                                capture,
                                shadows,
                                map_uploads,
                                map_response_delay_ms,
                                session,
                                command_session,
                                activity,
                            )
                        finally:
                            if command_session is not None:
                                commands.remove(metadata["client_id"], command_session)
                            if session is not None:
                                activity.end(session)
                else:
                    conn.sendall(unavailable_connack(metadata["protocol_version"]))
                    log(
                        "mqtt_rejected",
                        peer=peer[0],
                        reason="server unavailable",
                        meaning="diagnostic probe only; login not accepted",
                    )
            except (OSError, ProtocolError, sqlite3.Error, ValueError) as exc:
                if not handshake_logged:
                    log_handshake()
                log("mqtt_probe_failed", peer=peer[0], error=str(exc))
    finally:
        raw.close()


def tls_context(cert, key, max_tls="1.2"):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.maximum_version = {
        "1.2": ssl.TLSVersion.TLSv1_2,
        "1.3": ssl.TLSVersion.TLSv1_3,
    }[max_tls]
    context.load_cert_chain(cert, key)
    return context


def main():
    """Start the native-bridge listener with optional private capture/upload/command IPC.

    Daemon connection workers own individual transports; ExitStack owns the IPC
    server/capture resources. Normal deployments always provide shadow storage
    and accept local reported-state login, without claiming password validation.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cert", required=True, help="PEM certificate chain")
    parser.add_argument("--key", required=True, help="PEM private key")
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8883)
    parser.add_argument(
        "--capture-file",
        type=Path,
        help="opt-in private selected payload capture; new JSONL file in a mode-0700 directory",
    )
    parser.add_argument(
        "--capture-max-bytes",
        type=int,
        default=8 * 1024 * 1024,
        help="total capture file limit across reconnects (default 8 MiB)",
    )
    parser.add_argument(
        "--shadow-db",
        type=Path,
        required=True,
        help="private persistent reported-shadow SQLite database",
    )
    parser.add_argument(
        "--command-socket", type=Path, help="private Unix socket for HA vacuum commands"
    )
    parser.add_argument(
        "--map-upload-db",
        type=Path,
        help="opt-in map upload negotiation; shared database of the separate HTTPS receiver",
    )
    parser.add_argument(
        "--map-upload-base-url", help="reachable HTTPS receiver origin, e.g. https://your-host:8443"
    )
    parser.add_argument(
        "--map-response-delay-ms",
        type=int,
        default=0,
        help="diagnostic delay after request PUBACK before map response (0-2000 ms; default 0)",
    )
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    if bool(args.map_upload_db) != bool(args.map_upload_base_url):
        parser.error("--map-upload-db and --map-upload-base-url must be supplied together")
    if not 0 <= args.map_response_delay_ms <= 2000:
        parser.error("--map-response-delay-ms must be between 0 and 2000")
    if args.map_response_delay_ms and not args.map_upload_db:
        parser.error("--map-response-delay-ms requires --map-upload-db and --map-upload-base-url")
    if not 1 <= args.capture_max_bytes <= 64 * 1024 * 1024:
        parser.error("--capture-max-bytes must be between 1 and 67108864")
    bridge = Path(__file__).resolve().parents[1] / "tools/mqtt_tls_bridge"
    if not os.access(bridge, os.X_OK):
        parser.error(
            "Build the TLS helper: cc -O2 -Wall -Wextra -Werror "
            "native/mqtt_tls_bridge.c -o tools/mqtt_tls_bridge -lssl -lcrypto"
        )
    context = tls_context(args.cert, args.key)
    client_auth = (args.cert, args.key, bridge)
    shadows = ShadowService(args.shadow_db)
    map_uploads = (
        MapUploadService(args.map_upload_db, args.map_upload_base_url)
        if args.map_upload_db
        else None
    )
    with (
        contextlib.ExitStack() as resources,
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener,
    ):
        commands = None
        if args.command_socket:
            from roomba_cloud.robot_commands import CommandRegistry, CommandServer

            commands = CommandRegistry(args.shadow_db, args.map_upload_db)
            command_server = resources.enter_context(CommandServer(args.command_socket, commands))
            threading.Thread(target=command_server.serve_forever, daemon=True).start()
            resources.callback(command_server.shutdown)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((args.bind, args.port))
        capture = (
            resources.enter_context(PrivateCapture(args.capture_file, args.capture_max_bytes))
            if args.capture_file
            else None
        )
        if capture is not None:
            log(
                "mqtt_private_capture_enabled",
                path=str(args.capture_file),
                max_bytes=args.capture_max_bytes,
                mode="0600",
                warning="contains private robot payloads; do not share unreviewed",
            )
        maintenance = (
            resources.enter_context(SessionMaintenance(shadows)) if shadows is not None else None
        )
        listener.listen(16)
        log(
            "mqtt_probe_listening",
            bind=args.bind,
            port=listener.getsockname()[1],
            forwarding=False,
            accepts_login=True,
            min_tls="1.2",
            max_tls="1.2",
            requests_client_certificate=True,
            authenticates_client_issuer=False,
            shadow_service=shadows is not None,
            map_upload_service=map_uploads is not None,
            map_response_delay_ms=args.map_response_delay_ms,
            client_sigalgs="rsa_pkcs1_sha256",
        )
        while True:
            raw, peer = listener.accept()
            threading.Thread(
                target=handle,
                args=(
                    raw,
                    peer,
                    context,
                    10,
                    client_auth,
                    None,
                    capture,
                    shadows,
                    map_uploads,
                    args.map_response_delay_ms,
                    commands,
                    maintenance,
                ),
                daemon=True,
            ).start()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Stopped.", file=sys.stderr)
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
