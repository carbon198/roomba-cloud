import contextlib
import io
import json
from pathlib import Path
import socket
import ssl
import tempfile
import threading
import unittest
from unittest.mock import patch

import roomba_cloud.ha_mqtt_publish as ha
from roomba_cloud.mqtt_server import PacketReader, read_frame
from roomba_cloud.robot_mqtt import packet
from roomba_cloud.shadow_service import ShadowService
from roomba_cloud.cloud_http import make_cert


class HAPublisherTests(unittest.TestCase):
    def test_staleness_uses_field_timestamps_not_shadow_refresh(self):
        data = {"battery": 70, "mission_phase": "charge", "suction_level": 3}
        times = {"battery": 100, "mission_phase": 201}
        status = ha.field_availability(data, times, 100, 200)
        self.assertEqual(status["battery"], "online")
        self.assertEqual(status["mission_phase"], "offline")
        self.assertEqual(status["suction_level"], "offline")
        self.assertEqual(ha.field_availability(data, times, 99, 200)["battery"], "offline")

    def test_unchanged_settings_remain_valid_but_dynamic_fields_expire(self):
        data = {"battery": 70, "suction_level": 3, "child_lock": False}
        times = {"battery": 100, "suction_level": 100, "child_lock": 201}
        status = ha.field_availability(data, times, 99, 200)
        self.assertEqual(status["battery"], "offline")
        self.assertEqual(status["suction_level"], "online")
        self.assertEqual(status["child_lock"], "offline")
        self.assertEqual(ha.robot_availability(100, 100, 200), "online")
        for timestamp in (100, 201, None, True, "200"):
            self.assertEqual(ha.robot_availability(timestamp, 99, 200), "offline")
        self.assertEqual(ha.robot_availability(200, 99, 200), "online")

    def test_wrong_broker_hostname_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            cert, key = make_cert(directory, ["localhost"])
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert, key)
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                listener.listen()
                listener.settimeout(5)
                errors = []

                def server():
                    try:
                        raw, _ = listener.accept()
                        with raw:
                            with context.wrap_socket(raw, server_side=True):
                                pass
                    except ssl.SSLError:
                        pass
                    except Exception as exc:
                        errors.append(exc)

                thread = threading.Thread(target=server)
                thread.start()
                try:
                    with self.assertRaises(ssl.SSLCertVerificationError):
                        ha.Publisher(
                            {
                                "host": "127.0.0.1",
                                "port": listener.getsockname()[1],
                                "ca_file": str(cert),
                                "tls_server_name": "wrong.example",
                            },
                            "test",
                        )
                finally:
                    thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])

    def test_only_allowlisted_fields_and_exact_types(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            service = ShadowService(path)
            value = {
                "state": {
                    "reported": {
                        "batPct": 72,
                        "bin": {"present": True},
                        "cleanMissionStatus": {"phase": "charge", "missionId": "private"},
                        "dock": {"state": 353},
                        "runtimeStats": {"hr": 6928361},
                        "serial": "private",
                        "p2maps": [{"p2map_id": "private"}],
                    }
                }
            }
            service.respond(
                "$aws/things/" + "a" * 32 + "/shadow/name/ro-currentstate/update",
                json.dumps(value).encode(),
                "a" * 32,
            )
            data, timestamps, reported_at = ha.telemetry(path, "a" * 32)
            self.assertEqual(
                data,
                {
                    "battery": 72,
                    "bin_present": True,
                    "mission_phase": "charge",
                    "dock_state": 353,
                    "map_count": 1,
                },
            )
            self.assertEqual(set(timestamps), set(data))
            self.assertEqual(reported_at, timestamps["battery"])
            self.assertNotIn("private", json.dumps(data))
            self.assertEqual(ha.telemetry(path, "b" * 32), ({}, {}, None))

    def test_settings_update_does_not_refresh_robot_reporting_or_dynamic_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            service = ShadowService(path)
            robot_id = "a" * 32
            prefix = f"$aws/things/{robot_id}/shadow/name/"
            with patch("roomba_cloud.shadow_service.time.time", return_value=100):
                service.respond(
                    prefix + "ro-currentstate/update",
                    b'{"state":{"reported":{"batPct":70}}}',
                    robot_id,
                )
                service.respond(
                    prefix + "rw-settings/update",
                    b'{"state":{"reported":{"suctionLevel":3}}}',
                    robot_id,
                )
            with patch("roomba_cloud.shadow_service.time.time", return_value=200):
                service.respond(
                    prefix + "rw-settings/update",
                    b'{"state":{"reported":{"childLock":false}}}',
                    robot_id,
                )
            data, timestamps, reported_at = ha.telemetry(path, robot_id)
            self.assertEqual(reported_at, 100)
            self.assertEqual(ha.robot_availability(reported_at, 99, 200), "offline")
            with patch("roomba_cloud.shadow_service.time.time", return_value=200):
                service.respond(
                    prefix + "ro-currentstate/update",
                    b'{"state":{"reported":{"cleanMissionStatus":{"phase":"run"}}}}',
                    robot_id,
                )
            data, timestamps, reported_at = ha.telemetry(path, robot_id)
            self.assertEqual(ha.robot_availability(reported_at, 99, 200), "online")
            status = ha.field_availability(data, timestamps, 99, 200)
            self.assertEqual(status["suction_level"], "online")
            self.assertEqual(status["battery"], "offline")
            self.assertEqual(status["mission_phase"], "online")

    def test_config_permissions_and_required_trust(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text("{}")
            path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "private"):
                ha.load_config(path)
            path.chmod(0o600)
            with self.assertRaisesRegex(ValueError, "host"):
                ha.load_config(path)

    def test_expanded_telemetry_metadata_privacy_and_partial_updates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            service = ShadowService(path)
            robot_id = "a" * 32
            prefix = f"$aws/things/{robot_id}/shadow/name/"
            reports = {
                "ro-currentstate": {
                    "cleanMissionStatus": {
                        "sqft": 36,
                        "nMssn": 121,
                        "initiator": "matter",
                        "mssnStrtTm": 1792008993,
                        "notReady": 0,
                        "condNotReady": ["private"],
                        "operatingMode": 2,
                    },
                    "dock": {
                        "known": True,
                        "state": 353,
                        "pdState": 701,
                        "pwState": 601,
                        "error": 0,
                        "fwVer": "24",
                    },
                    "p2maps": [{"p2map_id": "private", "user_p2mapv_id": "private"}],
                },
                "rw-settings": {
                    "ecoCharge": False,
                    "twoPass": True,
                    "schedHold": False,
                    "padDryAllowed": 1,
                    "audio": {"volume": 0},
                    "name": "private",
                    "padDryDur": 3,
                },
                "ro-stats": {
                    "bbchg": {"nChgOk": 106, "nChgErr": 0},
                    "bbmssn": {"nMssn": 107, "nMssnOk": 65, "nMssnF": 9},
                    "bbrstinfo": {"nNavRst": 27},
                    "bbsys": {"hr": 5231},
                },
                "rw-software": {
                    "softwareVer": "test-version",
                    "subModSwVer": {
                        "con": "con-version",
                        "nav": "nav-version",
                        "mcu": "mcu-version",
                    },
                    "lastCommand": {
                        "command": "start",
                        "initiator": "matter",
                        "time": 1792008992,
                        "matterFabric": "private",
                        "regions": ["private"],
                    },
                },
            }
            with patch("roomba_cloud.shadow_service.time.time", return_value=100):
                for name, reported in reports.items():
                    service.respond(
                        prefix + name + "/update",
                        json.dumps({"state": {"reported": reported}}).encode(),
                        robot_id,
                    )
            with patch("roomba_cloud.shadow_service.time.time", return_value=200):
                service.respond(
                    prefix + "rw-settings/update",
                    b'{"state":{"reported":{"childLock":false}}}',
                    robot_id,
                )
            data, timestamps, _ = ha.telemetry(path, robot_id)
            for key, value in {
                "mission_area": 36,
                "mission_count": 121,
                "mission_initiator": "matter",
                "not_ready_conditions": 1,
                "map_count": 1,
                "dock_known": True,
                "dock_error": 0,
                "dock_pad_dry_state": 701,
                "dock_pad_wash_state": 601,
                "charge_success_count": 106,
                "charge_error_count": 0,
                "stats_mission_count": 107,
                "stats_mission_success_count": 65,
                "stats_mission_failure_count": 9,
                "navigation_reset_count": 27,
                "last_command": "start",
                "last_command_initiator": "matter",
                "firmware_version": "test-version",
                "mcu_firmware_version": "mcu-version",
                "eco_charge": False,
                "two_pass": True,
                "pad_dry_allowed": 1,
            }.items():
                self.assertEqual(data[key], value, key)
            self.assertEqual(data["mission_started"], "2026-10-14T20:16:33+00:00")
            self.assertEqual(data["last_command_time"], "2026-10-14T20:16:32+00:00")
            self.assertEqual(timestamps["eco_charge"], 100)
            self.assertEqual(timestamps["child_lock"], 200)
            self.assertNotIn("private", json.dumps(data))
            documents = [d for _, d in ha.discovery(robot_id, "test")]
            self.assertEqual(
                next(d for d in documents if d["name"] == "Mission cleaned area")[
                    "unit_of_measurement"
                ],
                "ft\u00b2",
            )
            self.assertEqual(
                next(d for d in documents if d["name"] == "Mission started")["device_class"],
                "timestamp",
            )
            self.assertTrue(all("command_topic" not in d for d in documents))
            status = ha.field_availability(data, timestamps, 99, 200)
            self.assertEqual(status["charge_success_count"], "online")
            self.assertEqual(status["firmware_version"], "online")
            self.assertEqual(status["mission_area"], "offline")

    def test_expanded_fields_reject_invalid_types_counts_and_timestamps(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            service = ShadowService(path)
            robot_id = "a" * 32
            service.respond(
                f"$aws/things/{robot_id}/shadow/name/ro-currentstate/update",
                b'{"state":{"reported":{"cleanMissionStatus":{"sqft":true,'
                b'"nMssn":-1,"mssnStrtTm":0,"condNotReady":"private"},'
                b'"p2maps":{"private":1}}}}',
                robot_id,
            )
            self.assertEqual(ha.telemetry(path, robot_id)[0], {})
            service.respond(
                f"$aws/things/{robot_id}/shadow/name/rw-software/update",
                b'{"state":{"reported":{"lastCommand":{"time":253402300800}}}}',
                robot_id,
            )
            self.assertEqual(ha.telemetry(path, robot_id)[0], {})

    def test_run_publishes_robot_timeout_without_expiring_cached_setting(self):
        config = {
            "robot_id": "a" * 32,
            "shadow_db": "unused",
            "stale_seconds": 100,
            "poll_seconds": 5,
        }
        base = "roomba_local/" + config["robot_id"]
        for now, expected in ((200, "online"), (201, "offline")):
            with (
                self.subTest(now=now),
                patch.object(ha, "Publisher") as publisher_class,
                patch.object(
                    ha, "telemetry", return_value=({"suction_level": 3}, {"suction_level": 1}, 100)
                ),
                patch.object(ha.time, "time", return_value=now),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                ha.run(config, once=True)
                publisher = publisher_class.return_value
                publisher.publish.assert_any_call(base + "/robot_availability", expected)
                publisher.publish.assert_any_call(base + "/available/suction_level", "online")
                publisher.close.assert_called_once()

    def test_retained_discovery_state_and_offline_over_verified_tls(self):
        self.check_publication(False)

    def test_explicit_insecure_tls_accepts_untrusted_wrong_hostname(self):
        self.check_publication(True)

    def check_publication(self, insecure):
        with tempfile.TemporaryDirectory() as directory:
            cert, key = make_cert(directory, ["localhost"])
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert, key)
            path = Path(directory) / "state.db"
            service = ShadowService(path)
            robot_id = "a" * 32
            service.respond(
                f"$aws/things/{robot_id}/shadow/name/ro-currentstate/update",
                b'{"state":{"reported":{"batPct":100}}}',
                robot_id,
            )
            config = {
                "host": "127.0.0.1",
                "ca_file": str(cert),
                "tls_server_name": "localhost",
                "username": "private-user",
                "password": "private-password",
                "robot_id": robot_id,
                "shadow_db": str(path),
                "poll_seconds": 5,
                "stale_seconds": 900,
            }
            if insecure:
                config.update(insecure_tls=True, ca_file="", tls_server_name="wrong.example")
            messages, errors = [], []
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                listener.listen()
                listener.settimeout(5)
                config["port"] = listener.getsockname()[1]

                def server():
                    try:
                        raw, _ = listener.accept()
                        with context.wrap_socket(raw, server_side=True) as conn:
                            buffer = bytearray()
                            header, body = read_frame(conn, 5, buffer)
                            self.assertEqual(header, 0x10)
                            self.assertIn(b"private-user", body)
                            self.assertIn(b"private-password", body)
                            conn.sendall(packet(0x20, b"\0\0"))
                            while True:
                                header, body = read_frame(conn, 5, buffer)
                                if header == 0xE0:
                                    break
                                self.assertEqual(header, 0x33)
                                reader = PacketReader(body)
                                topic = reader.text()
                                identifier = reader.identifier()
                                payload = reader.take(len(body) - reader.pos).decode()
                                messages.append((topic, payload))
                                conn.sendall(packet(0x40, identifier.to_bytes(2, "big")))
                    except Exception as exc:
                        errors.append(exc)

                thread = threading.Thread(target=server)
                thread.start()
                output = io.StringIO()
                try:
                    with contextlib.redirect_stdout(output):
                        ha.run(config, once=True)
                finally:
                    thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
            configs = [(t, json.loads(p)) for t, p in messages if t.startswith("homeassistant/")]
            self.assertEqual(len(configs), len(ha.FIELDS))
            self.assertTrue(all("command_topic" not in p for _, p in configs))
            base = "roomba_local/" + robot_id
            self.assertTrue(
                all(
                    p["availability_mode"] == "all"
                    and {"topic": base + "/robot_availability"} in p["availability"]
                    for _, p in configs
                )
            )
            self.assertEqual(
                next(p for t, p in configs if "_battery/" in t)["device_class"], "battery"
            )
            base = "roomba_local/" + robot_id
            self.assertIn((base + "/state", '{"battery": 100}'), messages)
            self.assertIn((base + "/available/battery", "online"), messages)
            self.assertIn((base + "/available/tank_present", "offline"), messages)
            self.assertIn((base + "/robot_availability", "online"), messages)
            self.assertEqual(messages[-1], (base + "/availability", "offline"))
            self.assertNotIn("private", output.getvalue())


if __name__ == "__main__":
    unittest.main()
