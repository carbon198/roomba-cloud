"""Synthetic deployment contracts, not permission to change a real deployment.

Cover configuration/token startup, certificate-mode conflicts, allowlisted build
sources, multi-robot isolation (including shared IP), registration and stale IPC.
The independent container_smoke script checks the packaged runtime. Physical
multi-robot behavior, live CA issuance and load/concurrency are not established
by these tests.
"""

import contextlib
import http.client
import io
import json
import os
import shlex
import signal
from pathlib import Path
import socket
import ssl
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, Mock, patch
from concurrent.futures import ThreadPoolExecutor

import roomba_cloud.cloud_http as cloud_http
import roomba_cloud.mqtt_server as mqtt_probe
import deployment.docker_entrypoint as docker_entrypoint
from roomba_cloud.robot_mqtt import mqtt_string, packet
from roomba_cloud.server import (
    Configuration,
    create_customer_token,
    healthcheck,
    HEALTHCHECK_CLIENT_ID,
    main,
    parse_arguments,
    serve_both,
)
from deployment.docker_entrypoint import arguments as container_arguments
from roomba_cloud.shadow_service import ShadowService
from roomba_cloud.robot_commands import (
    CommandRegistry,
    CommandServer,
    CommandSession,
    request_command,
)
from test_robot_commands import FakeRobot


A, B = "A" * 32, "B" * 32


class ConfigurationTests(unittest.TestCase):
    def test_combined_services_forward_configuration_and_fail_as_a_unit(self):
        with tempfile.TemporaryDirectory() as directory:
            args = parse_arguments(["both", *self.flags(directory)])
            https, mqtt = Mock(), Mock()
            https.pid, mqtt.pid = 100, 101
            https.poll.return_value = None
            mqtt.poll.return_value = 2
            with (
                patch("roomba_cloud.server.subprocess.Popen", side_effect=[https, mqtt]) as start,
                patch("roomba_cloud.server.os.killpg") as kill_group,
                self.assertRaisesRegex(RuntimeError, "mqtt service exited with status 2"),
            ):
                serve_both(args)
            for call, service in zip(start.call_args_list, ("https", "mqtt")):
                command = call.args[0]
                self.assertEqual(command[:4], [sys.executable, "-m", "roomba_cloud", service])
                child_args = parse_arguments(command[3:])
                self.assertEqual(child_args.data_dir, args.data_dir)
                self.assertEqual(child_args.tls_cert, args.tls_cert)
            self.assertEqual(kill_group.call_args_list[0].args, (100, signal.SIGTERM))
            self.assertEqual(kill_group.call_args_list[1].args, (101, signal.SIGTERM))
            self.assertTrue(all(call.kwargs["start_new_session"] for call in start.call_args_list))
            https.wait.assert_called_once_with(timeout=15)
            mqtt.wait.assert_called_once_with(timeout=15)

    def test_combined_services_clean_up_on_signal_and_partial_start_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            args = parse_arguments(["both", *self.flags(directory)])
            for failure in (SystemExit(0), KeyboardInterrupt(), OSError("start failed")):
                with self.subTest(failure=type(failure).__name__):
                    child = Mock()
                    child.pid = 100
                    child.poll.return_value = None
                    child.wait.side_effect = [subprocess.TimeoutExpired("service", 15), 0]
                    with (
                        patch("roomba_cloud.server.subprocess.Popen", side_effect=[child, failure]),
                        patch("roomba_cloud.server.os.killpg") as kill_group,
                        self.assertRaises(type(failure)),
                    ):
                        serve_both(args)
                    self.assertEqual(kill_group.call_args_list[0].args, (100, signal.SIGTERM))
                    self.assertEqual(kill_group.call_args_list[1].args, (100, signal.SIGKILL))
                    self.assertEqual(child.wait.call_count, 2)

    def test_docker_copy_sources_are_explicitly_allowed_in_build_context(self):
        root = Path(__file__).resolve().parents[1]
        allowed = set((root / ".dockerignore").read_text().splitlines())
        dockerfile = (root / "Dockerfile").read_text().replace("\\\n", "")
        for line in dockerfile.splitlines():
            words = shlex.split(line)
            if not words or words[0] != "COPY" or words[1].startswith("--from="):
                continue
            for source in words[1:-1]:
                with self.subTest(source=source):
                    self.assertTrue((root / source).exists())
                    self.assertIn("!" + source, allowed)
                    if (root / source).is_dir():
                        for child in (root / source).rglob("*"):
                            if child.is_file() and child.suffix in (".py", ".json"):
                                self.assertIn("!" + child.relative_to(root).as_posix(), allowed)

    def flags(self, directory, **overrides):
        value = {
            "hostname": "roomba.example.com",
            "tls_cert": str(Path(directory) / "cert.pem"),
            "tls_key": str(Path(directory) / "key.pem"),
            "customer_token_file": str(Path(directory) / "state/token"),
            "data_dir": str(Path(directory) / "state"),
            **overrides,
        }
        flags = []
        for key, value in value.items():
            if value is None or value is False:
                continue
            flags.append("--" + key.replace("_", "-"))
            if value is not True:
                flags.append(str(value))
        return flags

    def config(self, directory, **overrides):
        return parse_arguments(["https", *self.flags(directory, **overrides)])

    def test_config_builds_shared_persistent_state_and_listener_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Configuration(self.config(directory))
            config.prepare_state()
            self.assertEqual(config.data_dir.stat().st_mode & 0o777, 0o700)
            self.assertEqual((config.data_dir / "shadows.sqlite3").stat().st_mode & 0o777, 0o600)
            mqtt = config.arguments("mqtt")
            https = config.arguments("https")
            self.assertEqual(
                https[https.index("--customer-acme-challenge-file") + 1],
                str(config.data_dir / "dns-challenges/challenge.json"),
            )
            for flag in ("--command-socket", "--map-upload-db"):
                self.assertEqual(mqtt[mqtt.index(flag) + 1], https[https.index(flag) + 1])
            for args in (mqtt, https):
                self.assertNotIn("--robots-file", args)
                self.assertNotIn("--robot-ip", args)
            document = json.loads((config.data_dir / "discovery.json").read_text())
            self.assertEqual(document["mqtt"], "roomba.example.com")
            self.assertEqual(document["httpBase"], "https://roomba.example.com")
            self.assertEqual(document["vStream"], "https://roomba.example.com")

    def test_invalid_configuration_fails_instead_of_using_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            for changes in (
                {"hostname": "https://example.com"},
                {"hostname": "example.com/path"},
                {"hostname": "example.com:8443"},
                {"mqtt_port": 0},
                {"mqtt_port": 65536},
                {"https_port": 8883},
                {"data_dir": ""},
                {"customer_token_file": ""},
                {"hostname": "example.com:invalid"},
            ):
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    Configuration(self.config(directory, **changes))
            state = Path(directory) / "state"
            state.mkdir(mode=0o755)
            state.chmod(0o755)
            with self.assertRaisesRegex(ValueError, "private"):
                Configuration(self.config(directory)).prepare_state()

    def test_removed_json_and_legacy_overrides_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            for flag in (
                "--config",
                "--shadow-db",
                "--map-upload-db",
                "--command-socket",
                "--acme-domain",
            ):
                with (
                    self.subTest(flag=flag),
                    contextlib.redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit),
                ):
                    parse_arguments(["mqtt", *self.flags(directory), flag, "legacy"])

    def test_supplied_certificate_paths_and_acme_conflicts(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Configuration(self.config(directory, tls_cert="own.pem", tls_key="own.key"))
            self.assertEqual(config.tls_cert, Path.cwd() / "own.pem")
            self.assertEqual(config.tls_key, Path.cwd() / "own.key")
            with self.assertRaisesRegex(ValueError, "mutually exclusive"):
                Configuration(
                    self.config(directory, acme_email="me@example.com", acme_agree_tos=True)
                )
            for cert, key in (("own.pem", None), (None, "own.key"), ("", "own.key")):
                with self.subTest(cert=cert, key=key), self.assertRaisesRegex(ValueError, "both"):
                    Configuration(self.config(directory, tls_cert=cert, tls_key=key))
            Configuration(
                self.config(
                    directory,
                    tls_cert=None,
                    tls_key=None,
                    acme_email="me@example.com",
                    acme_agree_tos=True,
                )
            )

    def test_native_uses_only_flags_and_docker_translates_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = {
                "HOSTNAME": "roomba.example.com",
                "DATA_DIR": str(Path(directory) / "state"),
                "TLS_CERT": "env.pem",
                "TLS_KEY": "env.key",
                "ACME_EMAIL": "ignored-in-native@example.com",
            }
            native = ["mqtt", *self.flags(directory, tls_cert="cli.pem", tls_key="cli.key")]
            for command in (
                native,
                container_arguments(
                    ["mqtt"],
                    {key: value for key, value in environment.items() if key != "ACME_EMAIL"},
                ),
            ):
                with (
                    patch.dict(os.environ, environment, clear=True),
                    patch("sys.argv", ["server.py", *command]),
                    patch.object(Configuration, "prepare_state"),
                    patch("roomba_cloud.server.create_customer_token"),
                    patch.object(cloud_http.CustomerAuth, "load"),
                    patch("roomba_cloud.server.signal.signal"),
                    patch.object(mqtt_probe, "main") as serve,
                ):
                    main()
                    serve.assert_called_once()
                    expected = "cli.pem" if command == native else "env.pem"
                    self.assertEqual(
                        sys.argv[sys.argv.index("--cert") + 1], str(Path.cwd() / expected)
                    )
            for flags in (
                ["--acme-email", "me@example.com"],
                ["--acme-staging"],
                ["--acme-agree-tos"],
                ["--acme-email", "me@example.com"],
            ):
                with (
                    self.subTest(flags=flags),
                    patch("sys.argv", ["server.py", "mqtt", *self.flags(directory), *flags]),
                    patch.object(Configuration, "prepare_state") as prepare,
                    self.assertRaises(ValueError),
                ):
                    main()
                prepare.assert_not_called()

    def test_container_defaults_boolean_validation_and_native_defaults(self):
        args = parse_arguments(
            container_arguments(
                [],
                {
                    "HOSTNAME": "roomba.example.com",
                    "TLS_CERT": "/certs/cert.pem",
                    "TLS_KEY": "/certs/key.pem",
                    "ACME_AGREE_TOS": "false",
                },
            )
        )
        config = Configuration(args)
        self.assertEqual(args.service, "both")
        self.assertEqual(config.data_dir, Path("/data"))
        self.assertEqual(config.customer_token_file, Path("/data/customer-token.txt"))
        for name in ("ACME_AGREE_TOS", "ACME_STAGING"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "true or false"):
                container_arguments([], {name: "yes"})
        args = parse_arguments(
            [
                "https",
                "--hostname",
                "roomba.example.com",
                "--tls-cert",
                "cert.pem",
                "--tls-key",
                "key.pem",
            ]
        )
        config = Configuration(args)
        self.assertEqual(config.data_dir, Path.cwd() / "data")
        self.assertEqual(config.customer_token_file, config.data_dir / "customer-token.txt")

    def test_container_maps_all_settings_and_executes_native_cli(self):
        environment = {
            "HOSTNAME": "roomba.example.com",
            "DATA_DIR": "/data/custom",
            "CUSTOMER_TOKEN_FILE": "/data/custom token.txt",
            "BIND": "127.0.0.1",
            "HTTPS_PORT": "9443",
            "MQTT_PORT": "9883",
            "ACME_EMAIL": "me@example.com",
            "ACME_AGREE_TOS": "TRUE",
            "ACME_STAGING": "true",
        }
        flags = container_arguments(["healthcheck", "--check-service", "mqtt"], environment)
        args = parse_arguments(flags)
        config = Configuration(args)
        self.assertEqual(args.service, "healthcheck")
        self.assertEqual(args.check_service, "mqtt")
        self.assertEqual(config.data_dir, Path("/data/custom"))
        self.assertEqual(config.customer_token_file, Path("/data/custom token.txt"))
        self.assertEqual(config.bind, "127.0.0.1")
        self.assertEqual(config.ports, {"https": 9443, "mqtt": 9883})
        self.assertEqual(config.https_base_url, "https://roomba.example.com")
        self.assertEqual(args.acme_email, "me@example.com")
        self.assertTrue(args.acme_agree_tos)
        self.assertTrue(args.acme_staging)
        with (
            patch.dict(os.environ, environment, clear=True),
            patch("sys.argv", ["docker_entrypoint.py", "healthcheck", "--check-service", "mqtt"]),
            patch("deployment.docker_entrypoint.os.execv") as execute,
        ):
            docker_entrypoint.main()
        execute.assert_called_once_with(
            sys.executable,
            [sys.executable, "-m", "roomba_cloud", *flags],
        )
        for conflicts in (
            {"TLS_CERT": "/certs/cert.pem", "TLS_KEY": "/certs/key.pem"},
            {"TLS_CERT": "/certs/cert.pem"},
        ):
            with (
                self.subTest(conflicts=conflicts),
                self.assertRaisesRegex(ValueError, "mutually exclusive"),
            ):
                Configuration(
                    parse_arguments(container_arguments(["https"], {**environment, **conflicts}))
                )
        for variable in ("HOSTNAME", "HTTPS_PORT", "MQTT_PORT"):
            invalid = {**environment, variable: "" if variable == "HOSTNAME" else "bad"}
            with (
                self.subTest(variable=variable),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                parse_arguments(container_arguments(["https"], invalid))

    def test_healthcheck_does_not_create_state_or_token(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch(
                    "sys.argv",
                    ["server.py", "healthcheck", *self.flags(directory), "--check-service", "mqtt"],
                ),
                patch("roomba_cloud.server.healthcheck") as check,
                patch.object(Configuration, "prepare_state") as prepare,
            ):
                main()
            self.assertEqual(check.call_args.args[1], "mqtt")
            prepare.assert_not_called()
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_healthcheck_rejects_bad_protocol_responses(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Configuration(self.config(directory))
            for service, response in (
                ("https", b"HTTP/1.1 503 Unavailable\r\nContent-Length: 0\r\n\r\n"),
                ("https", b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\n\r\nx"),
                ("mqtt", mqtt_probe.unavailable_connack(4)),
                ("mqtt", packet(0xD0, b"")),
            ):
                with self.subTest(service=service, response=response):
                    context = MagicMock()
                    conn = context.wrap_socket.return_value.__enter__.return_value
                    conn.recv.return_value = response
                    conn.makefile.return_value = io.BytesIO(response)
                    with (
                        patch(
                            "roomba_cloud.server.ssl.create_default_context", return_value=context
                        ),
                        patch("roomba_cloud.server.socket.create_connection"),
                        self.assertRaises(RuntimeError),
                    ):
                        context.verify_flags = 0
                        healthcheck(config, service)

    def test_healthcheck_uses_managed_certificate_and_ipv6_loopback(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Configuration(
                self.config(
                    directory,
                    tls_cert=None,
                    tls_key=None,
                    acme_email="test@example.com",
                    acme_agree_tos=True,
                )
            )
            config.bind = "::"
            context = MagicMock()
            context.verify_flags = 0
            conn = context.wrap_socket.return_value.__enter__.return_value
            conn.recv.return_value = mqtt_probe.successful_connack(4)
            with (
                patch(
                    "roomba_cloud.server.ssl.create_default_context", return_value=context
                ) as tls,
                patch("roomba_cloud.server.socket.create_connection") as connect,
            ):
                healthcheck(config, "mqtt")
            tls.assert_called_once_with(
                cafile=str(config.data_dir / "acme/config/live/roomba.example.com/fullchain.pem")
            )
            connect.assert_called_once_with(("::1", 8883), timeout=1)

    def test_cli_token_path_preserves_symlink_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "actual-token"
            create_customer_token(target)
            link = Path(directory) / "token-link"
            link.symlink_to(target)
            config = Configuration(self.config(directory, customer_token_file=str(link)))
            self.assertEqual(config.customer_token_file, link)
            with self.assertRaises(OSError):
                create_customer_token(config.customer_token_file)

    def test_token_initialization_is_private_exclusive_and_never_prints_token(self):
        with tempfile.TemporaryDirectory() as directory:
            flags = self.flags(directory)
            output = io.StringIO()
            original_umask = os.umask(0o077)
            try:
                with (
                    patch("sys.argv", ["server.py", "init", *flags]),
                    contextlib.redirect_stdout(output),
                ):
                    main()
                token = Path(directory) / "state/token"
                self.assertEqual(token.stat().st_mode & 0o777, 0o600)
                self.assertNotIn(token.read_text().strip(), output.getvalue())
                self.assertIsNotNone(cloud_http.CustomerAuth.load(token))
                with (
                    patch("sys.argv", ["server.py", "init", *flags]),
                    self.assertRaises(FileExistsError),
                ):
                    main()
            finally:
                os.umask(original_umask)

    def test_first_service_start_creates_token_and_restart_preserves_it(self):
        with tempfile.TemporaryDirectory() as directory:
            flags = self.flags(directory)
            token_path = Path(directory) / "state/token"
            original_umask = os.umask(0o077)
            try:
                for service in ("mqtt", "https"):
                    module = mqtt_probe if service == "mqtt" else cloud_http
                    with (
                        patch("sys.argv", ["server.py", service, *flags]),
                        patch.object(module, "main") as serve,
                        contextlib.redirect_stdout(io.StringIO()),
                    ):
                        main()
                    serve.assert_called_once()
                    if service == "mqtt":
                        token = token_path.read_bytes()
                    else:
                        self.assertEqual(token_path.read_bytes(), token)
                self.assertEqual(token_path.stat().st_mode & 0o777, 0o600)
            finally:
                os.umask(original_umask)

    def test_concurrent_token_creation_and_invalid_existing_token(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token"
            with (
                contextlib.redirect_stdout(io.StringIO()),
                ThreadPoolExecutor(max_workers=8) as pool,
            ):
                list(pool.map(create_customer_token, [path] * 16))
            original = path.read_bytes()
            cloud_http.CustomerAuth.load(path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(Path(directory).iterdir()), [path])
            create_customer_token(path)
            self.assertEqual(path.read_bytes(), original)
            path.write_text("invalid")
            with self.assertRaises(ValueError):
                create_customer_token(path)
            self.assertEqual(path.read_text(), "invalid")

    def test_commands_route_to_one_robot_and_stale_socket_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "commands.sock"
            with socket.socket(socket.AF_UNIX) as stale:
                stale.bind(str(path))
                path.chmod(0o600)
            registry = CommandRegistry()
            robots = []
            for robot_id in (A, B):
                robot = FakeRobot()
                session = CommandSession(robot, 5, robot_id)
                robot.session = session
                session.subscriptions.add(session.topic)
                registry.register(robot_id, session)
                robots.append(robot)
            with CommandServer(path, registry) as server:
                with self.assertRaisesRegex(ValueError, "already has a listener"):
                    CommandServer(path, registry)
                thread = threading.Thread(target=server.serve_forever)
                thread.start()
                try:
                    request_command(path, A)
                    self.assertEqual(len(robots[0].payloads), 1)
                    self.assertEqual(robots[1].payloads, [])
                    request_command(path, B)
                    self.assertEqual(len(robots[0].payloads), 1)
                    self.assertIn(B, robots[1].payloads[0][0])
                finally:
                    server.shutdown()
                    thread.join(5)
            self.assertFalse(path.exists())


class MultiRobotNetworkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.cert, cls.key = cloud_http.make_cert(cls.directory.name, ["localhost"])
        cls.client_context = ssl._create_unverified_context()

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def health_config(self, service, port):
        return Configuration(
            parse_arguments(
                [
                    "healthcheck",
                    "--hostname",
                    "localhost",
                    "--tls-cert",
                    str(self.cert),
                    "--tls-key",
                    str(self.key),
                    "--data-dir",
                    str(Path(self.directory.name) / "unused"),
                    "--" + service + "-port",
                    str(port),
                ]
            )
        )

    def test_https_healthcheck_is_quiet_unauthenticated_and_does_not_use_storage(self):
        server = cloud_http.cloud_server()(("127.0.0.1", 0), cloud_http.cloud_handler())
        server.tls_context = mqtt_probe.tls_context(self.cert, self.key)
        server.uploads = None
        server.customer_auth = Mock()
        thread = threading.Thread(target=server.serve_forever)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            thread.start()
            try:
                healthcheck(self.health_config("https", server.server_port), "https")
                conn = http.client.HTTPSConnection(
                    "127.0.0.1",
                    server.server_port,
                    context=self.client_context,
                    timeout=3,
                )
                conn.request("POST", "/healthz")
                response = conn.getresponse()
                self.assertEqual(response.status, 405)
                response.read()
                conn.close()
                server.customer_auth.accepts.assert_not_called()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(5)
        self.assertEqual(output.getvalue(), "")

    def test_blocked_receiver_maintenance_does_not_block_https_or_shutdown(self):
        entered, release, offline = threading.Event(), threading.Event(), threading.Event()
        workers = set()

        def heartbeat(online=True):
            workers.add(threading.get_ident())
            if online:
                entered.set()
                release.wait(5)
            else:
                offline.set()

        server = cloud_http.cloud_server()(("127.0.0.1", 0), cloud_http.cloud_handler())
        server.tls_context = mqtt_probe.tls_context(self.cert, self.key)
        server.uploads = Mock(heartbeat=Mock(side_effect=heartbeat))
        server.maintenance_join_timeout = 0.05
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        with contextlib.redirect_stdout(io.StringIO()) as output:
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                for _ in range(3):
                    healthcheck(self.health_config("https", server.server_port), "https")
                self.assertEqual(server.uploads.heartbeat.call_count, 1)
                start = time.monotonic()
                server.shutdown()
                thread.join(1)
                self.assertFalse(thread.is_alive())
                self.assertLess(time.monotonic() - start, 1)
                self.assertIn("map_receiver_maintenance_stopping", output.getvalue())
            finally:
                release.set()
                server.shutdown()
                server.server_close()
                thread.join(2)
                self.assertTrue(offline.wait(2))
        self.assertEqual(len(workers), 1)
        self.assertEqual(server.uploads.heartbeat.call_count, 2)
        server.uploads.heartbeat.assert_called_with(False)

    def test_receiver_maintenance_retries_errors_and_stops_cleanly(self):
        failed, recovered, offline = threading.Event(), threading.Event(), threading.Event()

        def heartbeat(online=True):
            if not online:
                offline.set()
                raise sqlite3.OperationalError("offline locked")
            if not failed.is_set():
                failed.set()
                raise sqlite3.OperationalError("locked")
            recovered.set()

        server = cloud_http.cloud_server()(("127.0.0.1", 0), cloud_http.cloud_handler())
        server.uploads = Mock(heartbeat=Mock(side_effect=heartbeat))
        server.maintenance_interval = 0.05
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        with contextlib.redirect_stdout(io.StringIO()) as output:
            thread.start()
            try:
                self.assertTrue(recovered.wait(2))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(offline.is_set())
        self.assertIn("map_receiver_maintenance_failed", output.getvalue())
        self.assertIn("map_receiver_offline_failed", output.getvalue())
        calls = server.uploads.heartbeat.call_count
        time.sleep(0.1)
        self.assertEqual(server.uploads.heartbeat.call_count, calls)

    def test_mqtt_healthcheck_is_quiet_and_has_no_robot_side_effects(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            listener.settimeout(3)
            context = mqtt_probe.tls_context(self.cert, self.key)
            shadows, commands, capture, uploads = Mock(), Mock(), Mock(), Mock()

            def serve():
                raw, peer = listener.accept()
                mqtt_probe.handle(
                    raw,
                    peer,
                    context,
                    shadows=shadows,
                    commands=commands,
                    capture=capture,
                    map_uploads=uploads,
                )

            thread = threading.Thread(target=serve)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                thread.start()
                try:
                    healthcheck(self.health_config("mqtt", listener.getsockname()[1]), "mqtt")
                finally:
                    thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(output.getvalue(), "")
            for dependency in (shadows, commands, capture, uploads):
                self.assertEqual(dependency.mock_calls, [])

    def test_mqtt_healthcheck_rejects_robot_data_and_logs_failure(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            context = mqtt_probe.tls_context(self.cert, self.key)
            shadows = Mock()

            def serve():
                raw, peer = listener.accept()
                mqtt_probe.handle(raw, peer, context, shadows=shadows)

            thread = threading.Thread(target=serve)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                thread.start()
                try:
                    with socket.create_connection(listener.getsockname(), timeout=3) as raw:
                        with self.client_context.wrap_socket(
                            raw, server_hostname="localhost"
                        ) as conn:
                            conn.sendall(
                                packet(
                                    0x10,
                                    mqtt_string("MQTT")
                                    + b"\x04\x02\x00\x00"
                                    + mqtt_string(HEALTHCHECK_CLIENT_ID),
                                )
                            )
                            self.assertEqual(
                                mqtt_probe.read_frame(conn, 3, bytearray()), (0x20, b"\0\0")
                            )
                            conn.sendall(packet(0xC0, b""))
                            self.assertEqual(conn.recv(1), b"")
                finally:
                    thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertIn("mqtt_probe_failed", output.getvalue())
            self.assertEqual(shadows.mock_calls, [])

    def test_mqtt_healthcheck_identity_is_not_special_for_remote_peers(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            context = mqtt_probe.tls_context(self.cert, self.key)
            shadows = Mock()

            def serve():
                raw, peer = listener.accept()
                mqtt_probe.handle(raw, ("192.0.2.1", peer[1]), context, shadows=shadows)

            thread = threading.Thread(target=serve)
            with contextlib.redirect_stdout(io.StringIO()), patch.object(mqtt_probe, "observe"):
                thread.start()
                try:
                    with socket.create_connection(listener.getsockname(), timeout=3) as raw:
                        with self.client_context.wrap_socket(
                            raw, server_hostname="localhost"
                        ) as conn:
                            conn.sendall(
                                packet(
                                    0x10,
                                    mqtt_string("MQTT")
                                    + b"\x04\x02\x00\x00"
                                    + mqtt_string(HEALTHCHECK_CLIENT_ID),
                                )
                            )
                            self.assertEqual(
                                mqtt_probe.read_frame(conn, 3, bytearray()), (0x20, b"\0\0")
                            )
                finally:
                    thread.join(5)
            self.assertFalse(thread.is_alive())
            shadows.start_session.assert_called_once_with(
                HEALTHCHECK_CLIENT_ID,
                30,
                peer_ip="192.0.2.1",
            )
            shadows.end_session.assert_called_once()

    def test_healthcheck_detects_tls_listener_stall(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            with self.assertRaises(TimeoutError):
                healthcheck(self.health_config("https", listener.getsockname()[1]), "https")

    def test_healthcheck_validates_hostname(self):
        server = cloud_http.cloud_server()(("127.0.0.1", 0), cloud_http.cloud_handler())
        server.tls_context = mqtt_probe.tls_context(self.cert, self.key)
        server.uploads = None
        thread = threading.Thread(target=server.serve_forever)
        with contextlib.redirect_stdout(io.StringIO()):
            thread.start()
            try:
                config = self.health_config("https", server.server_port)
                config.hostname = "wrong.example.com"
                with self.assertRaises(ssl.SSLCertVerificationError):
                    healthcheck(config, "https")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(5)

    def test_https_accepts_unconfigured_robots_and_upload_tokens_from_any_peer(self):
        from roomba_cloud.maps.upload import MapUploadService

        with tempfile.TemporaryDirectory() as directory:
            uploads = MapUploadService(Path(directory) / "uploads.db")
            token = "a" * 64
            with uploads.connect() as db, db:
                db.execute(
                    "INSERT INTO uploads "
                    "(token, robot_id, xfer_type, xfer_id, expires, status) "
                    "VALUES (?, ?, 'uploadP2MapSmart', 1, 9999999999, 'pending')",
                    (token, B),
                )
            server = cloud_http.cloud_server()(("127.0.0.1", 0), cloud_http.cloud_handler())
            server.customer_auth = None
            server.uploads = uploads
            server.max_bytes, server.max_total = 1024, 4096
            server.discovery = b'{"httpBase":"https://roomba.example.com"}'
            server.registration_ok = True
            server.tls_context = mqtt_probe.tls_context(self.cert, self.key)
            thread = threading.Thread(target=server.serve_forever)
            with contextlib.redirect_stdout(io.StringIO()):
                thread.start()
                try:

                    def request(peer, method, path, body=b""):
                        conn = http.client.HTTPSConnection(
                            "127.0.0.1",
                            server.server_port,
                            context=self.client_context,
                            source_address=(peer, 0),
                            timeout=5,
                        )
                        try:
                            conn.request(method, path, body=body)
                            response = conn.getresponse()
                            response.read()
                            return response.status
                        finally:
                            conn.close()

                    for peer, robot_id in (("127.0.0.1", A), ("127.0.0.2", B)):
                        self.assertEqual(request(peer, "POST", f"/v1/{robot_id}/register"), 200)
                        self.assertEqual(request(peer, "GET", "/v1/robot/discover"), 200)
                    self.assertEqual(request("127.0.0.1", "POST", f"/v1/{B}/register"), 200)
                    self.assertEqual(request("127.0.0.3", "GET", "/v1/robot/discover"), 200)
                    self.assertEqual(
                        request("127.0.0.3", "PUT", "/map-upload/" + "b" * 64, b"x"), 404
                    )
                    self.assertEqual(request("127.0.0.3", "PUT", "/map-upload/" + token, b"x"), 200)
                    with uploads.connect() as db:
                        self.assertEqual(
                            db.execute(
                                "SELECT robot_id, status FROM uploads WHERE token=?", (token,)
                            ).fetchone(),
                            (B, "complete"),
                        )
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(5)

    def test_two_unconfigured_mqtt_robots_on_same_peer_isolate_shadows(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as resources:
            shadows = ShadowService(Path(directory) / "state.db")
            listener = resources.enter_context(socket.socket())
            listener.bind(("127.0.0.1", 0))
            listener.listen(4)
            listener.settimeout(5)
            context = mqtt_probe.tls_context(self.cert, self.key)
            threads = []

            def connect(peer, robot_id):
                raw = socket.create_connection(
                    listener.getsockname(), timeout=5, source_address=(peer, 0)
                )
                server_socket, address = listener.accept()
                thread = threading.Thread(
                    target=mqtt_probe.handle,
                    args=(server_socket, address, context),
                    kwargs={"shadows": shadows},
                )
                threads.append(thread)
                thread.start()
                conn = resources.enter_context(
                    self.client_context.wrap_socket(raw, server_hostname="localhost")
                )
                conn.sendall(
                    packet(0x10, mqtt_string("MQTT") + b"\x04\x02\x00\x1e" + mqtt_string(robot_id))
                )
                return conn

            with contextlib.redirect_stdout(io.StringIO()):
                try:
                    clients = [
                        (connect(peer, robot_id), robot_id, percent)
                        for peer, robot_id, percent in (("127.0.0.1", A, 21), ("127.0.0.1", B, 87))
                    ]
                    for conn, robot_id, percent in clients:
                        buffer = bytearray()
                        self.assertEqual(mqtt_probe.read_frame(conn, 5, buffer), (0x20, b"\0\0"))
                        topic = f"$aws/things/{robot_id}/shadow/name/ro-currentstate/update"
                        payload = json.dumps({"state": {"reported": {"batPct": percent}}}).encode()
                        conn.sendall(packet(0x30, mqtt_string(topic) + payload) + packet(0xC0))
                        self.assertEqual(mqtt_probe.read_frame(conn, 5, buffer), (0xD0, b""))
                    with contextlib.closing(sqlite3.connect(shadows.path)) as db:
                        rows = db.execute(
                            "SELECT topic, document FROM shadows ORDER BY topic"
                        ).fetchall()
                        self.assertEqual(len(rows), 2)
                        self.assertEqual(
                            [json.loads(row[1])["state"]["reported"]["batPct"] for row in rows],
                            [21, 87],
                        )
                    newcomer = connect("127.0.0.3", "C" * 32)
                    self.assertEqual(
                        mqtt_probe.read_frame(newcomer, 5, bytearray()), (0x20, b"\0\0")
                    )
                    newcomer.sendall(packet(0xE0))
                    newcomer.close()
                    for conn, _, _ in clients:
                        conn.sendall(packet(0xE0))
                        conn.close()
                finally:
                    resources.close()
                    for thread in threads:
                        thread.join(5)
                        self.assertFalse(thread.is_alive())
