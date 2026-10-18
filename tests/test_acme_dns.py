"""Mock DNS/Certbot lifecycle; no public records or live CA orders are created.

Validate challenge logging, resolver polling/timeout and certificate reloads.
These checks do not prove that a CA will see the same DNS data or issue a live
certificate; staging/production trials require user authorization.
"""

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import dns.resolver

import roomba_cloud.acme_dns as acme_dns
from roomba_cloud.server import Configuration, main
from deployment.docker_entrypoint import arguments as container_arguments
import test_deployment


class ACMETests(unittest.TestCase):
    def test_challenge_file_is_exact_and_cleanup_leaves_it_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "shared/challenge.json"
            resolver = Mock()
            resolver.resolve.return_value = [SimpleNamespace(strings=(b"a" * 43,))]
            with (
                self.challenge_environment(),
                patch.dict(os.environ, {"ROOMBA_ACME_CHALLENGE_FILE": str(path)}),
                patch("roomba_cloud.acme_dns.dns.resolver.Resolver", return_value=resolver),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                acme_dns.challenge()
                original = path.read_bytes()
                self.assertEqual(
                    json.loads(original),
                    {
                        "name": "_acme-challenge.robot.example.com",
                        "type": "TXT",
                        "value": "a" * 43,
                    },
                )
                self.assertEqual(path.stat().st_mode & 0o777, 0o644)
                acme_dns.challenge(cleanup=True)
                self.assertEqual(path.read_bytes(), original)
                acme_dns.write_challenge(path, "_acme-challenge.robot.example.com", "b" * 43)
                self.assertEqual(json.loads(path.read_bytes())["value"], "b" * 43)
                self.assertEqual(list(path.parent.iterdir()), [path])

    def test_failed_atomic_write_preserves_previous_request(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "challenge.json"
            path.write_text("previous")
            with (
                patch("roomba_cloud.acme_dns.os.replace", side_effect=OSError("test failure")),
                self.assertRaises(OSError),
            ):
                acme_dns.write_challenge(path, "record", "value")
            self.assertEqual(path.read_text(), "previous")
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_challenge_removed_only_after_successful_changed_certificate(self):
        for outcome in ("changed", "unchanged", "certbot-failed", "invalid-certificate"):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as directory:
                certificates = acme_dns.Certificates(
                    Path(directory), "robot.example.com", "me@example.com", True
                )
                acme_dns.write_challenge(certificates.challenge_file, "record", "value")
                original = certificates.challenge_file.read_bytes()
                with (
                    patch(
                        "roomba_cloud.acme_dns.fingerprint",
                        side_effect=[
                            b"old",
                            b"new" if outcome == "changed" else b"old",
                        ],
                    ),
                    patch("roomba_cloud.acme_dns.subprocess.run") as run,
                    patch("roomba_cloud.acme_dns.ssl.SSLContext") as context,
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    if outcome == "certbot-failed":
                        run.side_effect = subprocess.CalledProcessError(1, "certbot")
                    if outcome == "invalid-certificate":
                        context.return_value.load_cert_chain.side_effect = OSError("invalid pair")
                    if outcome in ("certbot-failed", "invalid-certificate"):
                        with self.assertRaises((subprocess.CalledProcessError, OSError)):
                            certificates.issue()
                    else:
                        certificates.issue()
                    self.assertEqual(
                        run.call_args.kwargs["env"]["ROOMBA_ACME_CHALLENGE_FILE"],
                        str(certificates.challenge_file),
                    )
                if outcome == "changed":
                    self.assertFalse(certificates.challenge_file.exists())
                else:
                    self.assertEqual(certificates.challenge_file.read_bytes(), original)

    def challenge_environment(self):
        return patch.dict(
            os.environ, {"CERTBOT_DOMAIN": "robot.example.com", "CERTBOT_VALIDATION": "a" * 43}
        )

    def test_logs_exact_record_and_requires_both_public_resolvers(self):
        resolver = Mock()
        resolver.resolve.side_effect = [
            [SimpleNamespace(strings=(b"a" * 20, b"a" * 23))],
            dns.resolver.NXDOMAIN(),
            [SimpleNamespace(strings=(b"a" * 43,))],
            [SimpleNamespace(strings=(b"a" * 43,))],
        ]
        output = io.StringIO()
        with (
            self.challenge_environment(),
            contextlib.redirect_stdout(output),
            patch("roomba_cloud.acme_dns.dns.resolver.Resolver", return_value=resolver),
            patch("roomba_cloud.acme_dns.time.sleep") as sleep,
        ):
            acme_dns.challenge()
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(events[0]["name"], "_acme-challenge.robot.example.com")
        self.assertEqual(events[0]["value"], "a" * 43)
        self.assertEqual(events[-1]["event"], "acme_dns_ready")
        self.assertEqual(resolver.resolve.call_count, 4)
        resolver.resolve.assert_called_with("_acme-challenge.robot.example.com.", "TXT", lifetime=5)
        sleep.assert_called_once()

    def test_wrong_txt_times_out_and_cleanup_never_queries_dns(self):
        resolver = Mock()
        resolver.resolve.return_value = [SimpleNamespace(strings=(b"wrong",))]
        with (
            self.challenge_environment(),
            contextlib.redirect_stdout(io.StringIO()),
            patch("roomba_cloud.acme_dns.dns.resolver.Resolver", return_value=resolver),
            patch("roomba_cloud.acme_dns.time.monotonic", side_effect=[0, 10]),
            self.assertRaises(TimeoutError),
        ):
            acme_dns.challenge(timeout=1)
        with (
            self.challenge_environment(),
            contextlib.redirect_stdout(io.StringIO()),
            patch("roomba_cloud.acme_dns.dns.resolver.Resolver") as resolve,
        ):
            acme_dns.challenge(cleanup=True)
            resolve.assert_not_called()

    def test_domain_and_explicit_account_consent_are_validated(self):
        self.assertEqual(acme_dns.domain_name("Robot.Example.com"), "robot.example.com")
        for name in (
            "*.example.com",
            "127.0.0.1",
            "localhost",
            "-bad.example.com",
            "example.com;echo",
            "https://example.com",
        ):
            with self.subTest(name=name), self.assertRaises(ValueError):
                acme_dns.domain_name(name)
        for email, agreed in ((None, True), ("bad", True), ("me@example.com", False)):
            with self.assertRaises(ValueError):
                acme_dns.Certificates(Path("/unused"), "robot.example.com", email, agreed)

    def test_issue_uses_persistent_account_and_hooks_without_dns_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            certificates = acme_dns.Certificates(
                Path(directory), "robot.example.com", "me@example.com", True, staging=True
            )
            with (
                patch("roomba_cloud.acme_dns.subprocess.run") as run,
                patch("roomba_cloud.acme_dns.ssl.SSLContext") as ssl_context,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                certificates.issue()
                command = run.call_args.args[0]
                self.assertIn("--staging", command)
                self.assertIn("--keep-until-expiring", command)
                self.assertIn("--manual-auth-hook", command)
                self.assertIn("--manual-cleanup-hook", command)
                self.assertIn(
                    "-m roomba_cloud.acme_dns hook",
                    command[command.index("--manual-auth-hook") + 1],
                )
                self.assertIn(str(Path(directory) / "acme-staging/config"), command)
                self.assertEqual(
                    run.call_args.kwargs["env"]["ROOMBA_ACME_LOG_PID"], str(os.getpid())
                )
                self.assertTrue(run.call_args.kwargs["check"])
                ssl_context.return_value.load_cert_chain.assert_called_once_with(
                    certificates.cert, certificates.key
                )

    def test_hook_log_bypasses_certbot_output_capture(self):
        code = """
import os, subprocess, sys
env = dict(os.environ, ROOMBA_ACME_LOG_PID=str(os.getpid()),
           CERTBOT_DOMAIN='robot.example.com', CERTBOT_VALIDATION='a'*43)
result = subprocess.run([sys.executable, '-m', 'roomba_cloud.acme_dns', 'hook', '--cleanup'],
                        env=env, capture_output=True, text=True, check=True)
assert not result.stdout, result.stdout
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        event = json.loads(result.stdout)
        self.assertEqual(event["event"], "acme_dns_cleanup")
        self.assertEqual(event["value"], "a" * 43)
        with tempfile.TemporaryFile(mode="w+") as output:
            output.write("existing log\n")
            output.flush()
            subprocess.run(
                [sys.executable, "-c", code],
                check=True,
                stdout=output,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            output.seek(0)
            self.assertTrue(output.read().startswith("existing log\n"))

    def test_certbot_failure_does_not_claim_certificate_success(self):
        with tempfile.TemporaryDirectory() as directory:
            certificates = acme_dns.Certificates(
                Path(directory), "robot.example.com", "me@example.com", True
            )
            with (
                patch(
                    "roomba_cloud.acme_dns.subprocess.run",
                    side_effect=subprocess.CalledProcessError(1, "certbot"),
                ),
                contextlib.redirect_stdout(io.StringIO()) as output,
                self.assertRaises(subprocess.CalledProcessError),
            ):
                certificates.issue()
            self.assertNotIn("acme_certificate_available", output.getvalue())

    def test_acme_configuration_derives_hostname_without_external_pems(self):
        with tempfile.TemporaryDirectory() as directory:
            args = test_deployment.ConfigurationTests().config(
                directory,
                hostname="robot.example.com",
                tls_cert=None,
                tls_key=None,
                acme_email="me@example.com",
                acme_agree_tos=True,
            )
            self.assertEqual(Configuration(args).hostname, "robot.example.com")
            args.hostname = "127.0.0.1"
            with self.assertRaisesRegex(ValueError, "DNS hostname"):
                Configuration(args)
            args.hostname = "robot.example.com"
            args.acme_email = None
            with self.assertRaises(ValueError):
                Configuration(args)

    def test_partial_acme_settings_are_rejected_without_supplied_certificates(self):
        with tempfile.TemporaryDirectory() as directory:
            for settings in (
                {"acme_email": "me@example.com"},
                {"acme_agree_tos": True},
                {"acme_staging": True},
            ):
                with self.subTest(settings=settings), self.assertRaisesRegex(ValueError, "both"):
                    Configuration(
                        test_deployment.ConfigurationTests().config(
                            directory, tls_cert=None, tls_key=None, **settings
                        )
                    )
            args = test_deployment.ConfigurationTests().config(
                directory,
                hostname="Robot.Example.com",
                tls_cert=None,
                tls_key=None,
                acme_email="me@example.com",
                acme_agree_tos=True,
            )
            config = Configuration(args)
            self.assertTrue(config.acme_enabled)
            self.assertEqual(config.hostname, "robot.example.com")
            self.assertEqual(config.https_base_url, "https://robot.example.com")

    def test_cli_and_environment_wire_certificate_management(self):
        with tempfile.TemporaryDirectory() as directory:
            args = test_deployment.ConfigurationTests().config(
                directory, hostname="robot.example.com"
            )
            config = Configuration(args)
            config.prepare_state()
            config.customer_token_file.write_text("a" * 64)
            config.customer_token_file.chmod(0o600)
            cli = test_deployment.ConfigurationTests().flags(
                directory,
                hostname="robot.example.com",
                tls_cert=None,
                tls_key=None,
                acme_email="me@example.com",
                acme_agree_tos=True,
            )
            docker = container_arguments(
                ["https"],
                {
                    "HOSTNAME": args.hostname,
                    "DATA_DIR": args.data_dir,
                    "CUSTOMER_TOKEN_FILE": args.customer_token_file,
                    "ACME_EMAIL": "me@example.com",
                    "ACME_AGREE_TOS": "true",
                },
            )
            for command in (["https", *cli], docker):
                with (
                    patch("sys.argv", ["server.py", *command]),
                    patch("roomba_cloud.server.signal.signal"),
                    patch("roomba_cloud.acme_dns.serve") as serve,
                ):
                    main()
                    runtime, service, certificates = serve.call_args.args
                    self.assertEqual(service, "https")
                    self.assertEqual(runtime.tls_cert, certificates.cert)
                    self.assertEqual(certificates.domain, "robot.example.com")

    def test_listener_reloads_changed_certificate_and_reaps_children(self):
        config = SimpleNamespace(tls_cert="cert", tls_key="key", arguments=Mock(return_value=[]))
        certificates = Mock()
        child = Mock()
        child.poll.return_value = None
        with (
            patch("roomba_cloud.acme_dns.fingerprint", side_effect=[b"old", b"new", b"new"]),
            patch("roomba_cloud.acme_dns.ssl.SSLContext"),
            patch(
                "roomba_cloud.acme_dns.subprocess.Popen", side_effect=[child, RuntimeError("stop")]
            ),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(RuntimeError, "stop"),
        ):
            acme_dns.serve(config, "https", certificates)
        certificates.issue.assert_called_once()
        child.terminate.assert_called_once()
        child.wait.assert_called_once_with(timeout=10)

    def test_mqtt_waits_for_https_issuance_without_registering_an_account(self):
        config = SimpleNamespace(tls_cert="cert", tls_key="key", arguments=Mock(return_value=[]))
        certificates = SimpleNamespace(domain="robot.example.com", issue=Mock())
        with (
            patch("roomba_cloud.acme_dns.fingerprint", side_effect=[None, b"ready", b"ready"]),
            patch("roomba_cloud.acme_dns.time.sleep") as sleep,
            patch("roomba_cloud.acme_dns.subprocess.Popen", side_effect=RuntimeError("stop")),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(RuntimeError, "stop"),
        ):
            acme_dns.serve(config, "mqtt", certificates)
        sleep.assert_called_once_with(2)
        certificates.issue.assert_not_called()

    def test_renewal_failure_keeps_listener_running_and_logs_retry(self):
        config = SimpleNamespace(tls_cert="cert", tls_key="key", arguments=Mock(return_value=[]))
        certificates = Mock()
        certificates.issue.side_effect = [None, subprocess.CalledProcessError(1, "certbot")]
        child = Mock()
        child.poll.side_effect = [None, 9]
        with (
            patch("roomba_cloud.acme_dns.fingerprint", return_value=b"same"),
            patch("roomba_cloud.acme_dns.time.monotonic", side_effect=[0, 50000, 50000]),
            patch("roomba_cloud.acme_dns.time.sleep"),
            patch("roomba_cloud.acme_dns.subprocess.Popen", return_value=child),
            contextlib.redirect_stdout(io.StringIO()) as output,
            self.assertRaisesRegex(RuntimeError, "status 9"),
        ):
            acme_dns.serve(config, "https", certificates)
        self.assertIn("acme_renewal_failed", output.getvalue())
        self.assertIn("one hour", output.getvalue())
        self.assertEqual(child.poll.call_count, 2)
