"""CLI defaults preserve the proven emulator path without old probe switches."""

import contextlib
import io
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import roomba_cloud.cloud_http as cloud_http
import roomba_cloud.mqtt_server as mqtt_probe
from roomba_cloud.robot_mqtt import packet


class EmulatorCLITests(unittest.TestCase):
    def test_package_entry_points(self):
        root = Path(__file__).resolve().parents[1]
        for module in (
            "roomba_cloud",
            "roomba_cloud.mqtt_server",
            "roomba_cloud.cloud_http",
            "roomba_cloud.robot_mqtt",
            "roomba_cloud.acme_dns",
            "provisioning.ble_provision",
            "provisioning.ble_probe",
            "deployment.docker_entrypoint",
        ):
            with self.subTest(module=module):
                result = subprocess.run(
                    [sys.executable, "-m", module, "--help"],
                    cwd=root,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=True,
                )
                self.assertIn("usage:", result.stdout)

    def test_https_defaults_to_trusted_tls_without_enrollment(self):
        with tempfile.TemporaryDirectory() as directory:
            cert, key = cloud_http.make_cert(directory, ["localhost"])
            token = Path(directory) / "token"
            token.write_text("a" * 64)
            token.chmod(0o600)
            server = SimpleNamespace(server_port=443, serve_forever=Mock())
            manager = Mock()
            manager.__enter__ = Mock(return_value=server)
            manager.__exit__ = Mock(return_value=False)
            factory = Mock(return_value=manager)
            args = [
                "cloud_http.py",
                "--cert",
                str(cert),
                "--key",
                str(key),
                "--customer-token-file",
                str(token),
            ]
            with (
                patch("sys.argv", args),
                patch("roomba_cloud.cloud_http.cloud_server", return_value=factory),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                cloud_http.main()
            self.assertTrue(server.registration_ok)
            self.assertEqual(server.tls_context.minimum_version, ssl.TLSVersion.TLSv1_2)
            self.assertIsNotNone(server.customer_auth)
            self.assertIsNotNone(server.discovery)
            server.serve_forever.assert_called_once()

    def test_mqtt_defaults_to_native_bridge_and_continuous_shadow_session(self):
        with tempfile.TemporaryDirectory() as directory:
            cert, key = cloud_http.make_cert(directory, ["localhost"])
            listener = Mock()
            listener.accept.side_effect = [(Mock(), ("192.0.2.10", 1234)), KeyboardInterrupt]
            listener.getsockname.return_value = ("0.0.0.0", 8883)
            manager = Mock()
            manager.__enter__ = Mock(return_value=listener)
            manager.__exit__ = Mock(return_value=False)
            args = [
                "roomba_cloud.mqtt_server",
                "--cert",
                str(cert),
                "--key",
                str(key),
                "--shadow-db",
                str(Path(directory) / "shadows.db"),
            ]
            with (
                patch("sys.argv", args),
                patch("roomba_cloud.mqtt_server.socket.socket", return_value=manager),
                patch("roomba_cloud.mqtt_server.threading.Thread") as thread,
                contextlib.redirect_stdout(io.StringIO()),
                self.assertRaises(KeyboardInterrupt),
            ):
                mqtt_probe.main()
            call_args = thread.call_args.kwargs["args"]
            self.assertEqual(call_args[4][:2], (str(cert), str(key)))
            self.assertEqual(call_args[4][2].name, "mqtt_tls_bridge")
            self.assertIsNone(call_args[5])
            self.assertIsInstance(call_args[7], mqtt_probe.ShadowService)
            self.assertEqual(call_args[2].maximum_version, ssl.TLSVersion.TLSv1_2)

    def test_no_artificial_mqtt_session_deadline(self):
        metadata = {"client_id": "a" * 32, "protocol_version": 4, "keepalive_seconds": 30}
        conn = Mock()
        with (
            patch("roomba_cloud.mqtt_server.time.monotonic", side_effect=[0, 4000]),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            mqtt_probe.observe(conn, "test", metadata, bytearray(packet(0xE0, b"")), None)
        conn.recv.assert_not_called()

    def test_continuous_handle_accepts_and_cleans_up_session(self):
        conn = Mock()
        conn.handshake.return_value = {"tls": "TLSv1.2"}
        conn.__enter__ = Mock(return_value=conn)
        conn.__exit__ = Mock(return_value=False)
        raw = Mock()
        shadows = Mock()
        metadata = {"client_id": "a" * 32, "protocol_version": 4, "keepalive_seconds": 30}
        with (
            patch(
                "roomba_cloud.mqtt_server.ClientCertificateTransport", return_value=conn
            ) as bridge,
            patch("roomba_cloud.mqtt_server.parse_client_hello", return_value={}),
            patch("roomba_cloud.mqtt_server.read_connect", return_value=metadata),
            patch("roomba_cloud.mqtt_server.observe") as observe,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            mqtt_probe.handle(
                raw,
                ("192.0.2.10", 1234),
                None,
                client_auth=("cert", "key", "bridge"),
                shadows=shadows,
            )
        bridge.assert_called_once_with(raw, "cert", "key", "bridge", 3600)
        conn.sendall.assert_called_once_with(mqtt_probe.successful_connack(4))
        self.assertIsNone(observe.call_args.args[4])
        shadows.start_session.assert_called_once_with("a" * 32, 65, peer_ip="192.0.2.10")
        shadows.end_session.assert_called_once_with(shadows.start_session.return_value)
        raw.close.assert_called_once()

    def test_removed_flags_are_absent_from_help(self):
        cases = (
            (
                cloud_http,
                (
                    "--host",
                    "--registration-ok",
                    "--min-tls",
                    "--max-tls",
                    "--show-secrets",
                    "--customer-stale-seconds",
                    "--robot-ip",
                    "--robots-file",
                ),
            ),
            (
                mqtt_probe,
                (
                    "--observe",
                    "--observe-seconds",
                    "--request-client-cert",
                    "--max-tls",
                    "--tls-bridge",
                    "--robot-ip",
                    "--robots-file",
                ),
            ),
        )
        for module, removed in cases:
            output = io.StringIO()
            with (
                patch("sys.argv", [module.__name__, "--help"]),
                contextlib.redirect_stdout(output),
                self.assertRaises(SystemExit) as caught,
            ):
                module.main()
            self.assertEqual(caught.exception.code, 0)
            for flag in removed:
                self.assertNotIn(flag, output.getvalue())


if __name__ == "__main__":
    unittest.main()
