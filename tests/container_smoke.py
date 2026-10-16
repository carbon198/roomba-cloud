"""Exercise runtime packaging without LAN ports, real robots, DNS or CA issuance.

Build the Dockerfile first, then run this script with --image and optionally
--engine podman. Unique names/one owned temporary volume prevent collisions;
cleanup removes only resources recorded by this invocation. A temporary local
certificate is trusted by the synthetic client, not a robot provisioning bypass.
The one root bootstrap container only prepares volume ownership; actual services
run as the image's non-root user with read-only roots and no capabilities.

Checks: native TLS MQTT CONNECT/PING, bearer HTTPS, persisted MQTT telemetry
visible through HTTPS, shared token/state, TLS/protocol healthchecks, abrupt MQTT crash
and restart, and Linux /proc hook log forwarding. This is not Compose orchestration
or a multi-physical-robot/load test. Rootless Podman passed on 2026-10-15;
Docker Engine/Compose runtime and real ACME issuance remain deployment-side trials.
"""

import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import time
import uuid


BOOTSTRAP = """
import json, os, pathlib, sys
root = pathlib.Path('/smoke')
data = json.load(sys.stdin)
for name, value in data.items():
    target = root / name
    target.write_text(value)
    target.chmod(0o600)
    os.chown(target, 10001, 10001)
state = root / 'state'
state.mkdir(mode=0o700)
os.chown(state, 10001, 10001)
"""

CHECK_HTTPS = """
import http.client, json, pathlib, ssl
context = ssl.create_default_context(cafile='/smoke/cert.pem')
conn = http.client.HTTPSConnection('localhost', 8443, context=context, timeout=3)
token = pathlib.Path('/smoke/state/token').read_text().strip()
conn.request('GET', '/v1/customer/session', headers={'Authorization': 'Bearer ' + token})
response = conn.getresponse()
assert response.status == 200, response.status
response.read()
conn.close()
conn = http.client.HTTPSConnection('localhost', 8443, context=context, timeout=3)
conn.request('GET', '/v1/customer/session')
response = conn.getresponse()
assert response.status == 401, response.status
response.read()
conn.close()
conn = http.client.HTTPSConnection('localhost', 8443, context=context, timeout=3)
conn.request('GET', '/v1/customer/robots/' + 'A' * 32 + '/telemetry',
             headers={'Authorization': 'Bearer ' + token})
response = conn.getresponse()
assert response.status == 200, response.status
assert json.loads(response.read())['values']['battery'] == 73
conn.close()
for route in ('/', '/dashboard.js', '/dashboard.css', '/v1/customer/robots'):
    conn = http.client.HTTPSConnection('localhost', 8443, context=context, timeout=3)
    conn.request('GET', route, headers={'Authorization': 'Bearer ' + token})
    response = conn.getresponse()
    assert response.status == 200, (route, response.status)
    body = response.read()
    if route == '/v1/customer/robots':
        robot = json.loads(body)['robots'][0]
        assert robot['robot_id'] == 'A' * 32
        assert robot['ip'] == '127.0.0.1'
        assert robot['battery'] == 73
    conn.close()
"""

CHECK_MQTT = """
import socket, ssl
from roomba_cloud.robot_mqtt import mqtt_string, packet
from roomba_cloud.mqtt_server import read_frame
context = ssl.create_default_context(cafile='/smoke/cert.pem')
with socket.create_connection(('127.0.0.1', 8883), timeout=3) as raw:
    with context.wrap_socket(raw, server_hostname='localhost') as conn:
        conn.sendall(packet(0x10, mqtt_string('MQTT') + b'\\x04\\x02\\x00\\x1e'
                            + mqtt_string('A' * 32)))
        assert read_frame(conn, 3, bytearray()) == (0x20, b'\\0\\0')
        topic = '$aws/things/' + 'A' * 32 + '/shadow/name/ro-currentstate/update'
        conn.sendall(packet(0x30, mqtt_string(topic)
                            + b'{"state":{"reported":{"batPct":73}}}'))
        conn.sendall(packet(0xc0))
        assert read_frame(conn, 3, bytearray()) == (0xd0, b'')
        conn.sendall(packet(0xe0))
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=("docker", "podman"), default="docker")
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    prefix = "roomba-smoke-" + uuid.uuid4().hex[:12]
    volume, mqtt, https = prefix + "-state", prefix + "-mqtt", prefix + "-https"
    created = []

    def run(*command, **kwargs):
        return subprocess.run(
            [args.engine, *command],
            check=True,
            capture_output=True,
            text=True,
            timeout=90,
            **kwargs,
        )

    def wait_for(container, code):
        deadline = time.monotonic() + 30
        while True:
            result = subprocess.run(
                [args.engine, "exec", container, "python", "-c", code],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                return
            if time.monotonic() >= deadline:
                raise RuntimeError(f"{container} failed smoke check: {result.stderr}")
            time.sleep(0.25)

    try:
        run("volume", "create", volume)
        created.append(volume)
        with tempfile.TemporaryDirectory() as directory:
            cert, key = Path(directory) / "cert.pem", Path(directory) / "key.pem"
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
                    "1",
                    "-subj",
                    "/CN=localhost",
                    "-addext",
                    "subjectAltName=DNS:localhost",
                ],
                check=True,
                capture_output=True,
                timeout=30,
            )
            run(
                "run",
                "--rm",
                "--network",
                "none",
                "--user",
                "0",
                "-i",
                "-v",
                volume + ":/smoke",
                "--entrypoint",
                "python",
                args.image,
                "-c",
                BOOTSTRAP,
                input=json.dumps({"cert.pem": cert.read_text(), "key.pem": key.read_text()}),
            )
        for name, service, network in (
            (mqtt, "mqtt", "none"),
            (https, "https", "container:" + mqtt),
        ):
            run(
                "run",
                "-d",
                "--name",
                name,
                "--network",
                network,
                "--read-only",
                "--tmpfs",
                "/tmp",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "-v",
                volume + ":/smoke",
                "-e",
                "TLS_CERT=/smoke/cert.pem",
                "-e",
                "TLS_KEY=/smoke/key.pem",
                "-e",
                "PUBLIC_URL=https://localhost:8443",
                "-e",
                "DATA_DIR=/smoke/state",
                "-e",
                "CUSTOMER_TOKEN_FILE=/smoke/state/token",
                args.image,
                service,
            )
            created.append(name)
            wait_for(name, CHECK_MQTT if service == "mqtt" else CHECK_HTTPS)
            probe = run(
                "exec",
                name,
                "python",
                "-m",
                "deployment.docker_entrypoint",
                "healthcheck",
                "--check-service",
                service,
            )
            assert not probe.stdout and not probe.stderr, probe
            logs = run("logs", name)
            assert "roomba-cloud-healthcheck" not in logs.stdout + logs.stderr
        hook_check = """
import os, subprocess, sys
env = dict(os.environ, ROOMBA_ACME_LOG_PID=str(os.getpid()),
           CERTBOT_DOMAIN='robot.example.com', CERTBOT_VALIDATION='a'*43)
result = subprocess.run([sys.executable, '-m', 'roomba_cloud.acme_dns', 'hook', '--cleanup'],
                        env=env, capture_output=True, text=True, check=True)
assert not result.stdout
"""
        hook_output = run("exec", https, "python", "-c", hook_check)
        assert json.loads(hook_output.stdout)["event"] == "acme_dns_cleanup"
        run("exec", mqtt, "python", "-c", "import os, signal; os.kill(1, signal.SIGKILL)")
        run("start", mqtt)
        wait_for(https, CHECK_HTTPS)
        wait_for(mqtt, CHECK_MQTT)
        run("stop", "--time", "25", https, mqtt)
        combined = prefix + "-combined"
        # Omit the command to verify that the image defaults to both services.
        run(
            "run",
            "-d",
            "--name",
            combined,
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/tmp",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "-v",
            volume + ":/smoke",
            "-e",
            "TLS_CERT=/smoke/cert.pem",
            "-e",
            "TLS_KEY=/smoke/key.pem",
            "-e",
            "PUBLIC_URL=https://localhost:8443",
            "-e",
            "DATA_DIR=/smoke/state",
            "-e",
            "CUSTOMER_TOKEN_FILE=/smoke/state/token",
            args.image,
        )
        created.append(combined)
        wait_for(combined, CHECK_MQTT)
        wait_for(combined, CHECK_HTTPS)
        run(
            "exec",
            combined,
            "python",
            "-c",
            r"""
import os, signal
from pathlib import Path
targets = [
    int(path.parent.name) for path in Path('/proc').glob('[0-9]*/cmdline')
    if b'roomba_cloud\x00mqtt\x00' in path.read_bytes()
]
assert len(targets) == 1, targets
os.kill(targets[0], signal.SIGKILL)
""",
        )
        deadline = time.monotonic() + 20
        while run("inspect", "--format", "{{.State.Running}}", combined).stdout.strip() == "true":
            if time.monotonic() >= deadline:
                raise RuntimeError("Combined container stayed running after MQTT died")
            time.sleep(0.25)
        assert run("inspect", "--format", "{{.State.ExitCode}}", combined).stdout.strip() == "1"
        run("start", combined)
        wait_for(combined, CHECK_MQTT)
        wait_for(combined, CHECK_HTTPS)
        run("stop", "--time", "25", combined)
        assert run("inspect", "--format", "{{.State.ExitCode}}", combined).stdout.strip() == "0"
        print(
            "Container smoke passed: non-root/read-only services, TLS MQTT CONNECT/PING, "
            "HTTPS auth, shared state, health checks, crash restart, and combined-service shutdown."
        )
    except (subprocess.SubprocessError, RuntimeError):
        for name in created[1:]:
            result = subprocess.run(
                [args.engine, "logs", "--tail", "30", name],
                capture_output=True,
                text=True,
                timeout=10,
            )
            print(result.stdout, result.stderr)
        raise
    finally:
        for name in reversed(created[1:]):
            subprocess.run(
                [args.engine, "rm", "-f", name], check=True, capture_output=True, timeout=30
            )
        if created:
            run("volume", "rm", volume)


if __name__ == "__main__":
    main()
