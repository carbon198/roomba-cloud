#!/usr/bin/env python3
"""Legacy read-only HA MQTT publisher and shared telemetry field definitions.

The native ha-roomba-local integration uses authenticated customer HTTPS/SSE;
this standalone publisher is an optional alternative, not the robot MQTT server
and not a command gateway. Its private --config JSON is intentionally separate
from server.py's CLI-only deployment configuration. Credentials belong only in
that local mode-0600 file; never in source or error logs.

FIELDS is also the base allowlist for customer_telemetry: wire shadow/path,
exact expected type and display label. Raw unknown enum meanings remain raw,
not guessed. Timestamps, count fields and individual freshness are handled
explicitly. Three availability levels separate publisher connectivity, robot
report freshness and field availability so retained stale state is not live.
"""

import argparse
import contextlib
from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import sqlite3
import ssl
import stat
import sys
import time

from roomba_cloud.mqtt_server import read_frame
from roomba_cloud.robot_mqtt import ProtocolError, mqtt_string, packet


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


def load_config(path):
    """Validate a private publisher configuration, including explicit broker TLS policy."""
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise ValueError("Configuration must be a regular private mode-0600 file")
    config = json.loads(path.read_text())
    if not isinstance(config, dict):
        raise ValueError("Configuration must be a JSON object")
    insecure = config.get("insecure_tls", False)
    if type(insecure) is not bool:
        raise ValueError("insecure_tls must be a boolean")
    required = ("host", "username", "password", "robot_id")
    if not insecure:
        required += ("tls_server_name", "ca_file")
    for key in required:
        if not isinstance(config.get(key), str) or not config[key].strip():
            raise ValueError(f"Fill in configuration field: {key}")
    if len(config["robot_id"]) != 32 or any(
        c not in "0123456789abcdefABCDEF" for c in config["robot_id"]
    ):
        raise ValueError("robot_id must contain 32 hexadecimal characters")
    if type(config.get("port")) is not int or not 1 <= config["port"] <= 65535:
        raise ValueError("port must be an integer between 1 and 65535")
    for key in ("poll_seconds", "stale_seconds"):
        if type(config.get(key)) is not int or not 1 <= config[key] <= 3600:
            raise ValueError(f"{key} must be an integer between 1 and 3600")
    if config["poll_seconds"] > 20:
        raise ValueError("poll_seconds must not exceed 20 (MQTT keepalive is 60)")
    if not isinstance(config.get("shadow_db"), str) or not config["shadow_db"].strip():
        raise ValueError("Fill in configuration field: shadow_db")
    for key in ("shadow_db",) + (("ca_file",) if not insecure else ()):
        config[key] = str((path.parent / config[key]).resolve())
    return config


def lookup(data, keys):
    for key in keys:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def telemetry(db_path, robot_id, fields=None):
    """Read a consistent shadow snapshot and return sanitized values, timestamps, report time.

    Exact type matching rejects bool-as-int. Lists become counts, not raw private
    contents. Missing/invalid observations stay absent. Current-state report time
    is distinct from each leaf's last update time.
    """
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


def discovery(robot_id, base):
    """Yield retained HA discovery in FIELDS order (customer field metadata relies on it)."""
    device = {
        "identifiers": ["roomba_local_" + robot_id],
        "name": "Roomba 505 local",
        "manufacturer": "iRobot",
        "model": "505 (local cloud telemetry)",
    }
    for key, (_, _, expected, label) in FIELDS.items():
        component = "binary_sensor" if expected is bool else "sensor"
        unique = "roomba_local_" + robot_id + "_" + key
        document = {
            "name": label,
            "unique_id": unique,
            "device": device,
            "state_topic": base + "/state",
            "value_template": "{{ value_json." + key + " | default(none) }}",
            "availability_mode": "all",
            "availability": [
                {"topic": base + "/availability"},
                {"topic": base + "/robot_availability"},
                {"topic": base + "/available/" + key},
            ],
        }
        if expected is bool:
            document["payload_on"] = "True"
            document["payload_off"] = "False"
        if key == "battery":
            document.update(
                device_class="battery", unit_of_measurement="%", state_class="measurement"
            )
        if key == "mission_area":
            document.update(unit_of_measurement="ft\u00b2", state_class="measurement")
        if key in TIMESTAMP_FIELDS:
            document["device_class"] = "timestamp"
        if key in COUNT_FIELDS:
            document["state_class"] = "measurement"
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
            document["entity_category"] = "diagnostic"
        yield "homeassistant/" + component + "/" + unique + "/config", document


def field_availability(data, timestamps, stale_seconds, now, fields=None):
    """Expire current-state leaves, not static settings/firmware, while rejecting future times."""
    fields = FIELDS if fields is None else fields
    return {
        key: "online"
        if key in data
        and timestamps.get(key) is not None
        and 0 <= now - timestamps[key]
        and (fields[key][0] != "ro-currentstate" or now - timestamps[key] <= stale_seconds)
        else "offline"
        for key in fields
    }


def robot_availability(reported_at, stale_seconds, now):
    return (
        "online"
        if type(reported_at) is int and 0 <= now - reported_at <= stale_seconds
        else "offline"
    )


class Publisher:
    """TLS-verified QoS-1 retained publishing with an offline last will.

    Explicit insecure_tls disables broker identity verification, not just a
    cosmetic warning: credentials could be intercepted. Publish offline during
    startup/exit; only advertise online after state and availability are written.
    """

    def __init__(self, config, base):
        self.buffer = bytearray()
        self.identifier = 0
        self.base = base
        if config.get("insecure_tls", False):
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        else:
            context = ssl.create_default_context(cafile=config["ca_file"])
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        raw = socket.create_connection((config["host"], config["port"]), timeout=10)
        try:
            self.conn = context.wrap_socket(
                raw, server_hostname=config.get("tls_server_name") or config["host"]
            )
        except BaseException:
            raw.close()
            raise
        try:
            # Clean session, retained offline will, username/password; MQTT 3.1.1.
            body = mqtt_string("MQTT") + b"\x04\xe6\0\x3c"
            body += mqtt_string("roomba-ha-" + config["robot_id"])
            body += mqtt_string(base + "/availability") + mqtt_string("offline")
            body += mqtt_string(config["username"]) + mqtt_string(config["password"])
            self.conn.sendall(packet(0x10, body))
            if read_frame(self.conn, 10, self.buffer) != (0x20, b"\0\0"):
                raise ProtocolError("Home Assistant broker rejected MQTT CONNECT")
        except BaseException:
            self.conn.close()
            raise

    def publish(self, topic, value):
        payload = value if isinstance(value, str) else json.dumps(value, allow_nan=False)
        self.identifier = self.identifier % 65535 + 1
        identifier = self.identifier.to_bytes(2, "big")
        self.conn.settimeout(10)
        self.conn.sendall(packet(0x33, mqtt_string(topic) + identifier + payload.encode()))
        if read_frame(self.conn, 10, self.buffer) != (0x40, identifier):
            raise ProtocolError("Home Assistant broker did not acknowledge publication")

    def close(self):
        try:
            self.publish(self.base + "/availability", "offline")
            self.conn.sendall(packet(0xE0))
        finally:
            self.conn.close()


def run(config, once=False):
    base = "roomba_local/" + config["robot_id"]
    publisher = Publisher(config, base)
    try:
        publisher.publish(base + "/availability", "offline")
        for topic, document in discovery(config["robot_id"], base):
            publisher.publish(topic, document)
        print(
            json.dumps(
                {
                    "event": "ha_mqtt_connected",
                    "entities": len(FIELDS),
                    "verifies_server_certificate": not config.get("insecure_tls", False),
                }
            ),
            flush=True,
        )
        while True:
            data, timestamps, reported_at = telemetry(config["shadow_db"], config["robot_id"])
            now = time.time()
            publisher.publish(base + "/state", data)
            for key, status in field_availability(
                data, timestamps, config["stale_seconds"], now
            ).items():
                publisher.publish(base + "/available/" + key, status)
            publisher.publish(
                base + "/robot_availability",
                robot_availability(reported_at, config["stale_seconds"], now),
            )
            publisher.publish(base + "/availability", "online")
            print(json.dumps({"event": "ha_telemetry_published", "fields": len(data)}), flush=True)
            if once:
                return
            time.sleep(config["poll_seconds"])
    finally:
        publisher.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--once", action="store_true", help="publish once and mark offline on exit")
    args = parser.parse_args()
    config = load_config(args.config)
    if config.get("insecure_tls", False):
        print(
            json.dumps(
                {
                    "event": "ha_tls_verification_disabled",
                    "warning": "Encrypted connection does not authenticate broker; credentials could be intercepted",
                }
            ),
            file=sys.stderr,
            flush=True,
        )
    while True:
        try:
            run(config, args.once)
            return
        except (OSError, sqlite3.Error, ProtocolError, ValueError) as exc:
            # Never print exception text from authentication or private database data.
            print(
                json.dumps(
                    {
                        "event": "ha_publish_failed",
                        "error_type": type(exc).__name__,
                        "tls_verify_code": exc.verify_code
                        if isinstance(exc, ssl.SSLCertVerificationError)
                        else None,
                        "message": "Check broker TLS/authentication, ACLs and database access locally",
                    }
                ),
                file=sys.stderr,
                flush=True,
            )
            if args.once:
                raise SystemExit(1) from None
            time.sleep(10)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Stopped.", file=sys.stderr)
    except (OSError, ValueError) as exc:
        print(
            f"Configuration error: {type(exc).__name__}; check private configuration fields and paths",
            file=sys.stderr,
        )
        sys.exit(1)
