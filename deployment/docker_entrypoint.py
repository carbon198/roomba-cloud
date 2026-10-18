#!/usr/bin/env python3
"""Adapt Docker environment to server.py; do not duplicate configuration policy.

The image defaults to both services; Compose may select separate HTTPS/MQTT
containers. Both modes run as UID/GID 10001 with a read-only
root filesystem, shared writable /data and optional read-only /certs. That user
must be able to traverse/read certificate paths; host ACME symlinks pointing
outside the mounted tree do not work. Changing DATA_DIR does not move the volume.
HTTPS_HOST_PORT/MQTT_HOST_PORT belong to Compose publishing, not this adapter.
Keep persistent state/token paths on the shared volume; removing that volume
destroys tokens, history and maps. BLE provisioning belongs on the host, not here.
"""

import os
import sys


def arguments(command, environment):
    """Translate values for both service launch and Compose healthchecks.

    Empty ordinary values use the Docker default or omit the flag. Booleans
    accept only true/false (case-insensitive; empty is false). server.py owns
    hostname validation and supplied-certificate versus ACME exclusivity.
    """
    result = list(command or ["both"])
    defaults = {"DATA_DIR": "/data", "BIND": "0.0.0.0", "HTTPS_PORT": "8443", "MQTT_PORT": "8883"}
    for name in (
        "HOSTNAME",
        "DATA_DIR",
        "CUSTOMER_TOKEN_FILE",
        "BIND",
        "HTTPS_PORT",
        "MQTT_PORT",
        "TLS_CERT",
        "TLS_KEY",
        "ACME_EMAIL",
    ):
        value = environment.get(name) or defaults.get(name)
        if value is not None:
            result.extend(("--" + name.lower().replace("_", "-"), value))
    for name in ("ACME_AGREE_TOS", "ACME_STAGING"):
        value = environment.get(name, "false").lower() or "false"
        if value not in ("true", "false"):
            raise ValueError(f"{name} must be true or false")
        if value == "true":
            result.append("--" + name.lower().replace("_", "-"))
    return result


def main():
    """Replace the adapter so container signals reach the actual launcher."""
    flags = arguments(sys.argv[1:], os.environ)
    os.execv(sys.executable, [sys.executable, "-m", "roomba_cloud", *flags])


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as exc:
        print(f"Container configuration failed: {exc}", file=sys.stderr)
        sys.exit(1)
