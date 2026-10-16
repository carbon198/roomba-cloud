"""Manual public-DNS certificate validation; no DNS-provider credentials.

Only HTTPS issues/renews. MQTT waits for the same certificate in shared private
storage, then both supervisors restart their child listener on certificate
changes. Existing listeners stay up while renewal waits for a human TXT change.
Check every 12 hours, retry failed renewals after one hour. Manual DNS validation
is NOT unattended renewal: watch logs and add each newly requested TXT value.
Alternatively, an external updater can read dns-challenges/challenge.json under
the data directory. It contains only name/type/value, is replaced atomically on
new challenges, and is deleted only after successful changed certificate loading.
Failed attempts and Certbot cleanup hooks leave the file unchanged. Share only
the challenge directory, not private state, with the updater.
Do not restart a pending challenge; a new attempt can replace its value.

Outbound ACME HTTPS and public DNS UDP/TCP 53 are needed; inbound Internet ports
are not. The single non-wildcard domain is derived from the public URL hostname
by server.py. Staging state is isolated under acme-staging rather than acme, but
staging certificates are untrusted by robots/HA. Preserve private account,
certificate and renewal state; recreating containers is not a renewal strategy
and repeated production issuance is subject to CA rate limits.

The live hook log forwarding uses Linux /proc, so the server deployment platform
is Linux even though the separate BLE provisioning tool supports other hosts.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import ssl
import subprocess
import sys
import tempfile
import time

import dns.exception
import dns.resolver


def log(event, **fields):
    print(json.dumps({"event": event, **fields}), flush=True)


def domain_name(value):
    if not isinstance(value, str):
        raise ValueError("ACME domain must be a DNS hostname")
    name = value.encode("idna").decode("ascii").lower()
    if (
        len(name) > 253
        or "." not in name
        or not all(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in name.split(".")
        )
    ):
        raise ValueError("ACME domain must be a fully qualified DNS hostname, not a wildcard")
    import ipaddress

    try:
        ipaddress.ip_address(name)
    except ValueError:
        return name
    raise ValueError("ACME requires a DNS hostname, not an IP address")


def write_challenge(path, record, value):
    """Atomically publish public DNS data, never private ACME account/key state."""
    path = Path(path)
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".challenge-", delete=False) as target:
        temporary = Path(target.name)
        try:
            target.write(
                (json.dumps({"name": record, "type": "TXT", "value": value}) + "\n").encode()
            )
            target.flush()
            os.fchmod(target.fileno(), 0o644)
            os.fsync(target.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def challenge(cleanup=False, timeout=1800, interval=15):
    """Log one exact TXT value and wait for both independent public resolvers.

    Poll Cloudflare and Google every 15 seconds for at most 30 minutes. Resolver
    visibility is only a prerequisite: the CA can still reject validation.
    Normal DNS resolution follows CNAME delegation. Cleanup names only this TXT
    value, never instructing the operator to delete other concurrent challenges.
    """
    domain = domain_name(os.environ["CERTBOT_DOMAIN"])
    value = os.environ["CERTBOT_VALIDATION"]
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", value):
        raise ValueError("Invalid DNS-01 TXT challenge value")
    record = "_acme-challenge." + domain
    if cleanup:
        log(
            "acme_dns_cleanup",
            name=record,
            type="TXT",
            value=value,
            instruction="Validation attempt finished; remove only this TXT value.",
        )
        return
    challenge_file = os.environ.get("ROOMBA_ACME_CHALLENGE_FILE")
    if challenge_file:
        write_challenge(challenge_file, record, value)
        log("acme_dns_challenge_file", path=challenge_file)
    log(
        "acme_dns_challenge",
        name=record,
        type="TXT",
        value=value,
        instruction="Add this TXT value in public DNS. Keep other TXT values. "
        "Waiting for both public resolvers; validation will continue automatically.",
        timeout_seconds=timeout,
    )
    deadline = time.monotonic() + timeout
    while True:
        ready = True
        for address in ("1.1.1.1", "8.8.8.8"):
            resolver = dns.resolver.Resolver(configure=False)
            resolver.nameservers = [address]
            try:
                answer = resolver.resolve(record + ".", "TXT", lifetime=5)
                found = any(b"".join(item.strings).decode("ascii") == value for item in answer)
            except (dns.exception.DNSException, UnicodeDecodeError) as exc:
                log("acme_dns_pending", resolver=address, reason=type(exc).__name__)
                found = False
            ready = ready and found
        if ready:
            log("acme_dns_ready", name=record)
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("DNS-01 TXT record did not propagate before the deadline")
        time.sleep(min(interval, remaining))


class Certificates:
    """Keep Certbot configuration, account keys and lineage under private state."""

    def __init__(self, data_dir, domain, email, agree_tos, staging=False):
        self.domain = domain_name(domain)
        if not email or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            raise ValueError("ACME requires --acme-email / ACME_EMAIL")
        if not agree_tos:
            raise ValueError("ACME requires explicit --acme-agree-tos / ACME_AGREE_TOS=true")
        self.email = email
        self.staging = staging
        self.root = Path(data_dir) / ("acme-staging" if staging else "acme")
        self.cert = self.root / "config/live" / self.domain / "fullchain.pem"
        self.key = self.cert.with_name("privkey.pem")
        self.challenge_file = Path(data_dir).absolute() / "dns-challenges/challenge.json"

    def issue(self):
        """Remove a published challenge only after a changed, loadable certificate pair.

        Certbot's cleanup hook also runs on failed validations and is too early
        to confirm issuance. Leave the JSON untouched on failure/no certificate
        change; the next authorization atomically replaces it with its new value.
        """
        previous = fingerprint(self.cert, self.key)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Certbot executes hooks through a shell; quote interpreter/source paths.
        import shlex

        hook = " ".join(
            shlex.quote(part) for part in (sys.executable, "-m", "roomba_cloud.acme_dns", "hook")
        )
        command = [
            sys.executable,
            "-c",
            "from certbot.main import main; raise SystemExit(main())",
            "certonly",
            "--non-interactive",
            "--agree-tos",
            "--email",
            self.email,
            "--manual",
            "--preferred-challenges",
            "dns",
            "--manual-auth-hook",
            hook,
            "--manual-cleanup-hook",
            hook + " --cleanup",
            "--config-dir",
            str(self.root / "config"),
            "--work-dir",
            str(self.root / "work"),
            "--logs-dir",
            str(self.root / "logs"),
            "--cert-name",
            self.domain,
            "-d",
            self.domain,
            "--keep-until-expiring",
        ]
        if self.staging:
            command.append("--staging")
        log(
            "acme_certificate_check",
            domain=self.domain,
            staging=self.staging,
            warning="Staging certificates are not trusted by robots or Home Assistant."
            if self.staging
            else "Manual DNS changes are required for new authorizations.",
        )
        # Hooks must inherit stdout: Certbot otherwise captures their challenge output.
        environment = {
            **os.environ,
            "ROOMBA_ACME_LOG_PID": str(os.getpid()),
            "ROOMBA_ACME_CHALLENGE_FILE": str(self.challenge_file),
        }
        subprocess.run(command, check=True, env=environment)
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_context.load_cert_chain(self.cert, self.key)
        current = fingerprint(self.cert, self.key)
        if current is not None and current != previous:
            self.challenge_file.unlink(missing_ok=True)
        log("acme_certificate_available", domain=self.domain)


def fingerprint(cert, key):
    """Hash contents (following Certbot symlinks), not only names or timestamps."""
    try:
        return hashlib.sha256(Path(cert).read_bytes() + Path(key).read_bytes()).digest()
    except FileNotFoundError:
        return None


def serve(config, service, certificates):
    """Supervise listeners and reload only a complete, loadable certificate pair.

    Renewal runs while the existing child serves traffic. A valid change
    terminates that child and starts another; active connections may reconnect.
    Initial HTTPS issuance blocks startup, while initial MQTT waits for files.
    Listener crashes fail explicitly rather than becoming an unbounded hidden
    restart loop. Shutdown reaps only the child owned by this supervisor.
    """
    if service == "https":
        certificates.issue()
    else:
        log("acme_waiting_for_certificate", domain=certificates.domain)
        while fingerprint(config.tls_cert, config.tls_key) is None:
            time.sleep(2)
    next_check = time.monotonic() + 12 * 3600
    while True:
        revision = fingerprint(config.tls_cert, config.tls_key)
        child = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "roomba_cloud.cloud_http" if service == "https" else "roomba_cloud.mqtt_server",
                *config.arguments(service),
            ]
        )
        try:
            while True:
                status = child.poll()
                if status is not None:
                    raise RuntimeError(f"{service} listener exited with status {status}")
                if service == "https" and time.monotonic() >= next_check:
                    try:
                        certificates.issue()
                    except (OSError, ValueError, subprocess.SubprocessError) as exc:
                        log(
                            "acme_renewal_failed",
                            error_type=type(exc).__name__,
                            recovery="Existing listener remains active; retrying in one hour.",
                        )
                        next_check = time.monotonic() + 3600
                    else:
                        next_check = time.monotonic() + 12 * 3600
                current = fingerprint(config.tls_cert, config.tls_key)
                if current is not None and current != revision:
                    try:
                        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                        context.load_cert_chain(config.tls_cert, config.tls_key)
                    except (OSError, ValueError) as exc:
                        log("acme_certificate_reload_pending", error_type=type(exc).__name__)
                        time.sleep(2)
                        continue
                    log("acme_listener_reload", service=service)
                    break
                time.sleep(2)
        finally:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("hook",))
    parser.add_argument("--cleanup", action="store_true")
    args = parser.parse_args()
    # Certbot captures hook stdout/stderr. /proc/PID/fd/1 points at the
    # invoking server's log stream, so users see TXT records before validation.
    parent = os.environ.get("ROOMBA_ACME_LOG_PID")
    if parent:
        fd = os.open(f"/proc/{int(parent)}/fd/1", os.O_WRONLY | os.O_APPEND)
        try:
            os.dup2(fd, 1)
        finally:
            os.close(fd)
    challenge(cleanup=args.cleanup)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TimeoutError) as exc:
        log("acme_hook_failed", error_type=type(exc).__name__)
        raise SystemExit(1) from None
