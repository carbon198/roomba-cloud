#!/usr/bin/env python3
"""Compose the HTTPS and MQTT services from one validated CLI configuration.

Deployment contract:
* Native configuration is CLI-only; docker_entrypoint translates environment
  variables to these flags. Historical JSON configs are neither read nor removed.
* Run both services as the same user with the same private data directory.
  shadows.sqlite3, uploads.sqlite3, commands.sock, telemetry wakeups, upload
  archives and the customer token form one deployment's private state. Stop both
  services before a consistent backup; preserve archive paths relative to the DB.
  Do not share writable state with a second deployment or mixed schema versions.
* public_url is the externally reachable HTTPS origin, not necessarily the bind
  port. Discovery advertises only an MQTT hostname: robots still expect external
  TCP 8883 even if the internal MQTT listener port changes.
* Supply a trusted full PEM chain and matching unencrypted private key to both
  listeners, or let the HTTPS ACME supervisor manage shared certificates.
  DNS redirection alone does not provision the robot or bypass certificate trust.

This is a LAN emulator, tested physically on a 505 running
p25-505+9.3.6+I3.8.149, not a full cloud replacement. Robot IDs are self-asserted;
there is no enrollment/IP allowlist or MQTT-password authentication. Firewall
robot-facing ports to trusted devices. The separate customer bearer key grants
access to all robots in this deployment, not a per-robot tenant boundary.
"""

import argparse
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit


class Configuration:
    """Validate once and derive matching flags for the existing protocol daemons.

    Paths resolve against the launcher's working directory. No robot-specific
    configuration is needed: MQTT client IDs and registrations discover robots.
    Configuration is startup-only; URL or supplied-PEM changes require restarting
    both services. Managed ACME reloads are handled separately by acme_dns.
    """

    def __init__(self, args):
        self.acme_enabled = bool(args.acme_email or args.acme_agree_tos or args.acme_staging)
        if self.acme_enabled:
            if args.tls_cert is not None or args.tls_key is not None:
                raise ValueError(
                    "Supplied TLS certificates and ACME options are mutually exclusive"
                )
            if not args.acme_email or not args.acme_agree_tos:
                raise ValueError("ACME requires both --acme-email and --acme-agree-tos")
        else:
            if not args.tls_cert or not args.tls_key:
                raise ValueError(
                    "Supply both --tls-cert and --tls-key, or both --acme-email and --acme-agree-tos"
                )
        origin = args.public_url
        if not isinstance(origin, str):
            raise ValueError("public_url must be an HTTPS origin")
        url = urlsplit(origin)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.path not in ("", "/")
            or url.query
            or url.fragment
            or url.port == 0
            or any(c.isspace() for c in origin)
        ):
            raise ValueError("public_url must be an HTTPS origin without credentials")
        self.public_url = origin.rstrip("/")
        self.hostname = url.hostname
        if self.acme_enabled:
            from roomba_cloud.acme_dns import domain_name

            self.hostname = domain_name(self.hostname)
        self.bind = args.bind
        if not isinstance(self.bind, str):
            raise ValueError("bind must be a string")
        self.ports = {}
        for service in ("https", "mqtt"):
            port = getattr(args, service + "_port")
            if type(port) is not int or not 1 <= port <= 65535:
                raise ValueError("Listener ports must be integers between 1 and 65535")
            self.ports[service] = port
        if self.ports["https"] == self.ports["mqtt"]:
            raise ValueError("HTTPS and MQTT ports must be different")
        for key in ("data_dir", "tls_cert", "tls_key"):
            if self.acme_enabled and key in ("tls_cert", "tls_key"):
                continue
            raw = getattr(args, key)
            if not isinstance(raw, str) or not raw:
                raise ValueError(f"{key} must be a nonempty path")
            setattr(self, key, Path(raw).absolute())
        if args.customer_token_file is not None and not args.customer_token_file:
            raise ValueError("customer_token_file must be a nonempty path")
        self.customer_token_file = (
            Path(args.customer_token_file).absolute()
            if args.customer_token_file is not None
            else self.data_dir / "customer-token.txt"
        )

    def prepare_state(self):
        """Create private storage, refusing insecure existing paths rather than chmodding them."""
        self.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.data_dir.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
            raise ValueError("data_dir must be a private directory (mode 0700)")
        from roomba_cloud.shadow_service import ShadowService

        ShadowService(self.data_dir / "shadows.sqlite3")

    def discovery(self):
        """Return the tested V4 discovery fields, all pointing at this local deployment."""
        return {
            "awsRegion": "us-east-1",
            "discoveryTTL": 60,
            "httpBase": self.public_url,
            "iotTopics": "$aws",
            "irbtTopics": "v011-irbthbu",
            "svcDeplId": "v011",
            "mqtt": self.hostname,
            "mqttApp": self.hostname,
            "mqttAts": self.hostname,
            "userServicesBase": self.hostname,
            "vStream": self.public_url,
        }

    def arguments(self, service):
        """Keep upload storage, IPC and robot/customer views on the same databases."""
        common = [
            "--cert",
            str(self.tls_cert),
            "--key",
            str(self.tls_key),
            "--bind",
            self.bind,
            "--port",
            str(self.ports[service]),
            "--map-upload-db",
            str(self.data_dir / "uploads.sqlite3"),
            "--command-socket",
            str(self.data_dir / "commands.sock"),
        ]
        if service == "mqtt":
            return common + [
                "--shadow-db",
                str(self.data_dir / "shadows.sqlite3"),
                "--map-upload-base-url",
                self.public_url,
            ]
        discovery = self.data_dir / "discovery.json"
        discovery.write_text(json.dumps(self.discovery()) + "\n")
        discovery.chmod(0o600)
        return common + [
            "--discovery-file",
            str(discovery),
            "--customer-token-file",
            str(self.customer_token_file),
            "--customer-shadow-db",
            str(self.data_dir / "shadows.sqlite3"),
        ]


def create_customer_token(path, *, exclusive=False):
    """Publish a private complete token without overwriting another service's winner.

    Concurrent first starts use an atomic hard link, then validate the winning
    file. Existing invalid/insecure tokens fail explicitly; they are never silently
    rotated. Optional ``init`` is exclusive even when a valid token already exists.
    Only the path is printed, never the token itself.
    """
    from roomba_cloud.cloud_http import CustomerAuth

    if not exclusive:
        try:
            CustomerAuth.load(path)
        except FileNotFoundError:
            pass
        else:
            return
    # Publish a complete file atomically so concurrent services never read a partial token.
    with tempfile.NamedTemporaryFile(
        dir=path.parent, prefix=".customer-token-", delete=False
    ) as target:
        temporary = Path(target.name)
        try:
            target.write((secrets.token_urlsafe(48) + "\n").encode())
            target.flush()
            os.fsync(target.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                if exclusive:
                    raise
                CustomerAuth.load(path)
            else:
                print(
                    f"Created private customer token file at {path}. Keep its contents secret.",
                    flush=True,
                )
        finally:
            temporary.unlink()


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("service", choices=("both", "https", "mqtt", "init", "healthcheck"))
    parser.add_argument(
        "--public-url", required=True, help="public HTTPS origin reachable by robots and HA"
    )
    parser.add_argument(
        "--data-dir", default="data", help="private state directory (default: ./data)"
    )
    parser.add_argument(
        "--customer-token-file",
        help="customer token path (default: DATA_DIR/customer-token.txt; created on first run)",
    )
    parser.add_argument("--bind", default="0.0.0.0", help="listener address (default: 0.0.0.0)")
    parser.add_argument(
        "--https-port", type=int, default=8443, help="HTTPS listener port (default: 8443)"
    )
    parser.add_argument(
        "--mqtt-port", type=int, default=8883, help="MQTT listener port (default: 8883)"
    )
    parser.add_argument("--check-service", choices=("https", "mqtt"), default="https")
    parser.add_argument("--tls-cert", help="own PEM certificate chain path; incompatible with ACME")
    parser.add_argument("--tls-key", help="own PEM private key path; requires --tls-cert")
    parser.add_argument(
        "--acme-email", help="enable ACME for the public URL hostname with this email"
    )
    parser.add_argument(
        "--acme-agree-tos", action="store_true", help="accept the certificate authority terms"
    )
    parser.add_argument(
        "--acme-staging",
        action="store_true",
        help="use untrusted Let's Encrypt staging certificates",
    )
    return parser.parse_args(argv)


def serve_both(args):
    """Own both service launchers; fail as a unit instead of hiding a dead service.

    Each launcher owns a process group, including Certbot and its hook workers.
    Signal both groups before waiting so pending DNS validation cannot orphan
    workers. Allow ACME supervisors time to reap their listeners.
    The container runtime, not this process, is responsible for restart policy.
    """
    flags = []
    for name, value in vars(args).items():
        if name in ("service", "check_service") or value is None or value is False:
            continue
        flags.append("--" + name.replace("_", "-"))
        if value is not True:
            flags.append(str(value))
    children = []
    try:
        for service in ("https", "mqtt"):
            children.append(
                (
                    service,
                    subprocess.Popen(
                        [sys.executable, "-m", "roomba_cloud", service, *flags],
                        start_new_session=True,
                    ),
                )
            )
        while True:
            for service, child in children:
                status = child.poll()
                if status is not None:
                    raise RuntimeError(f"{service} service exited with status {status}")
            time.sleep(0.5)
    finally:
        for _, child in children:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass  # The entire owned group has already exited.
        for _, child in children:
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait()


def main():
    """Dispatch configured services; healthcheck tests TCP reachability only.

    A passing healthcheck does not prove robot connectivity, TLS trust or API
    health. SIGTERM exits through normal cleanup. ACME mode supervises children;
    supplied-PEM mode invokes the daemon in this process.
    """
    args = parse_arguments()
    config = Configuration(args)
    if args.service == "healthcheck":
        host = "127.0.0.1" if config.bind == "0.0.0.0" else config.bind
        with socket.create_connection((host, config.ports[args.check_service]), timeout=3):
            return
    os.umask(0o077)
    config.prepare_state()
    if args.service == "init":
        create_customer_token(config.customer_token_file, exclusive=True)
        return
    create_customer_token(config.customer_token_file)
    from roomba_cloud.cloud_http import CustomerAuth

    CustomerAuth.load(config.customer_token_file)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    if args.service == "both":
        serve_both(args)
        return
    if config.acme_enabled:
        from roomba_cloud.acme_dns import Certificates, serve

        certificates = Certificates(
            config.data_dir,
            config.hostname,
            args.acme_email,
            args.acme_agree_tos,
            args.acme_staging,
        )
        config.tls_cert, config.tls_key = certificates.cert, certificates.key
        serve(config, args.service, certificates)
        return
    sys.argv = [args.service, *config.arguments(args.service)]
    if args.service == "mqtt":
        from roomba_cloud.mqtt_server import main as serve
    else:
        from roomba_cloud.cloud_http import main as serve
    serve()


def cli():
    """Run the launcher with consistent command-line error reporting."""
    try:
        main()
    except KeyboardInterrupt:
        pass
    except (
        OSError,
        ValueError,
        TypeError,
        RuntimeError,
        sqlite3.Error,
        subprocess.SubprocessError,
    ) as exc:
        print(f"Deployment failed: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    cli()
