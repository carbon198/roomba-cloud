#!/usr/bin/env python3
"""Direct-to-robot MQTT client plus shared MQTT framing primitives.

This is NOT the cloud emulator listener; roomba_cloud.server/mqtt_server serve that role.
UDP discovery identifies LAN robots without cleaning; listen records private
telemetry. Command subcommands dry-run unless --execute is supplied and use the
robot's LOCAL cmd topic, not the cloud v011-irbthbu/things/<id>/cmd topic.
Sending QoS 0 proves only a transport write; verify execution with Matter/robot.

Local TLS uses openssl s_client for V4 signature-algorithm compatibility.
Explicit --ca-file authenticates the IP SAN; --insecure is an acknowledgement
that encryption does not authenticate the peer. This trust choice does not
bypass the robot's trust of an emulated cloud's server certificate.
Keep passwords, output and captures private. Packet helpers are reused by
server code and bound frames to 1 MiB, including fragmented/coalesced reads.
"""

import argparse
import base64
import ipaddress
import json
import math
import os
from pathlib import Path
import select
import socket
import struct
import subprocess
import sys
import tempfile
import time


MAX_PACKET = 1024 * 1024


class ProtocolError(Exception):
    pass


def mqtt_string(value):
    data = value.encode("utf-8")
    if not data or len(data) > 65535 or b"\0" in data:
        raise ValueError("MQTT strings must be nonempty, NUL-free, and <=65535 bytes")
    return struct.pack("!H", len(data)) + data


def packet(header, body=b""):
    if len(body) > MAX_PACKET:
        raise ValueError("MQTT packet exceeds the 1 MiB diagnostic limit")
    length = len(body)
    encoded = bytearray([header])
    while True:
        digit = length % 128
        length //= 128
        encoded.append(digit | (128 if length else 0))
        if not length:
            return bytes(encoded) + body


def extract_packet(buffer):
    """Return one complete frame, preserving partial frames across timeouts."""
    if len(buffer) < 2:
        return None
    length = 0
    for index in range(1, min(len(buffer), 5)):
        digit = buffer[index]
        length += (digit & 127) * (128 ** (index - 1))
        if not digit & 128:
            if length > MAX_PACKET:
                raise ProtocolError("Incoming MQTT packet exceeds 1 MiB")
            end = index + 1 + length
            if len(buffer) < end:
                return None
            frame = buffer[0], bytes(buffer[index + 1 : end])
            del buffer[:end]
            return frame
    if len(buffer) >= 5:
        raise ProtocolError("Malformed MQTT remaining length")
    return None


def command_payload(command, mop=False, wetness=2):
    """Build captured local command fields; default combo wetness is explicit, not inferred."""
    result = {
        "command": command,
        "time": int(time.time()),
        "initiator": "localApp",
    }
    if command == "start":
        result["params"] = {"operatingMode": 6 if mop else 2}
        if mop:
            result["params"]["padWetness"] = {
                "disposable": wetness,
                "reusable": wetness,
            }
    return result


class TLS:
    """Use OpenSSL's supported CLI to configure V4 signature algorithms."""

    def __init__(self, ip, port, timeout, ca_file=None, insecure=False):
        if not ca_file and not insecure:
            raise ValueError("Choose --ca-file or explicitly acknowledge --insecure")
        address = ipaddress.ip_address(ip)
        target = f"[{address}]:{port}" if address.version == 6 else f"{address}:{port}"
        args = [
            "openssl",
            "s_client",
            "-quiet",
            "-ign_eof",
            "-connect",
            target,
            "-tls1_2",
            "-sigalgs",
            "RSA+SHA256:RSA+SHA384:RSA+SHA512",
            "-cipher",
            "ECDHE-RSA-AES256-GCM-SHA384:DEFAULT",
            "-legacy_server_connect",
        ]
        if ca_file:
            args += ["-CAfile", str(ca_file), "-verify_return_error", "-verify_ip", ip]
        self.timeout = timeout
        self.buffer = bytearray()
        self.errors = tempfile.TemporaryFile()
        try:
            self.process = subprocess.Popen(
                args,
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
        detail = self.errors.read(8192).decode("utf-8", errors="replace").strip()
        return ConnectionError(f"TLS connection closed. OpenSSL diagnostics: {detail or '(none)'}")

    def send(self, data):
        deadline = time.monotonic() + self.timeout
        while data:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([], [self.process.stdin], [], remaining)[1]:
                raise TimeoutError("Timed out writing to the TLS transport")
            try:
                count = os.write(self.process.stdin.fileno(), data)
            except BlockingIOError:
                continue
            except BrokenPipeError as exc:
                raise self.failure() from exc
            data = data[count:]

    def receive(self, timeout=None):
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        while True:
            frame = extract_packet(self.buffer)
            if frame is not None:
                return frame
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.process.stdout], [], [], remaining)[0]:
                raise TimeoutError("Timed out waiting for an MQTT packet")
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise self.failure()
            self.buffer.extend(chunk)

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.process.stdin.close()
        self.process.stdout.close()
        self.errors.close()


class MQTT:
    """Minimal local MQTT 3.1.1 client, not a durable command/retry queue."""

    def __init__(self, transport):
        self.transport = transport
        self.packet_id = 0

    def connect(self, blid, password):
        body = (
            mqtt_string("MQTT")
            + b"\x04\xc2\x00\x1e"
            + mqtt_string(blid)
            + mqtt_string(blid)
            + mqtt_string(password)
        )
        self.transport.send(packet(0x10, body))
        header, response = self.transport.receive()
        if header != 0x20 or len(response) != 2 or response[0] != 0:
            raise ProtocolError("Invalid CONNACK for clean-session MQTT connection")
        if response[1]:
            reasons = {
                1: "protocol version rejected",
                2: "client ID rejected",
                3: "server unavailable",
                4: "bad username/password",
                5: "not authorized",
            }
            raise ProtocolError(f"MQTT login rejected: {reasons.get(response[1], response[1])}")

    def incoming(self, header, body):
        if header >> 4 != 3:
            raise ProtocolError(f"Unexpected MQTT packet type 0x{header:02x}")
        qos = (header >> 1) & 3
        if qos not in (0, 1):
            raise ProtocolError("Incoming QoS 2/3 is not supported by this diagnostic client")
        if len(body) < 2:
            raise ProtocolError("Truncated PUBLISH topic length")
        length = struct.unpack("!H", body[:2])[0]
        end = 2 + length
        if not length or len(body) < end + (2 if qos else 0):
            raise ProtocolError("Truncated or empty PUBLISH topic")
        topic = body[2:end].decode("utf-8")
        if "\0" in topic or "+" in topic or "#" in topic:
            raise ProtocolError("Invalid PUBLISH topic")
        if qos:
            identifier = body[end : end + 2]
            if identifier == b"\0\0":
                raise ProtocolError("Invalid PUBLISH packet identifier")
            self.transport.send(packet(0x40, identifier))
            end += 2
        payload = body[end:]
        print(
            json.dumps(
                {
                    "event": "publish",
                    "timestamp": time.time(),
                    "topic": topic,
                    "qos": qos,
                    "retained": bool(header & 1),
                    "duplicate": bool(header & 8),
                    "payload_base64": base64.b64encode(payload).decode("ascii"),
                    "payload_text": payload.decode("utf-8", errors="replace"),
                }
            ),
            flush=True,
        )

    def subscribe(self, topic):
        self.packet_id += 1
        identifier = struct.pack("!H", self.packet_id)
        self.transport.send(packet(0x82, identifier + mqtt_string(topic) + b"\x01"))
        deadline = time.monotonic() + self.transport.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Timed out waiting for SUBACK: {topic}")
            header, body = self.transport.receive(remaining)
            if header == 0x90:
                if len(body) != 3 or body[:2] != identifier or body[2] not in (0, 1):
                    raise ProtocolError(f"Subscription rejected or invalid SUBACK: {topic}")
                print(f"Subscription accepted: {topic} (QoS {body[2]})", file=sys.stderr)
                return
            self.incoming(header, body)

    def listen(self, duration):
        end = time.monotonic() + duration
        next_ping = time.monotonic() + 10
        ping_deadline = None
        while time.monotonic() < end:
            now = time.monotonic()
            if ping_deadline is not None and now >= ping_deadline:
                raise TimeoutError("Robot did not answer MQTT PINGREQ")
            if ping_deadline is None and now >= next_ping:
                self.transport.send(packet(0xC0))
                ping_deadline = time.monotonic() + self.transport.timeout
            try:
                header, body = self.transport.receive(min(1, max(0, end - time.monotonic())))
            except TimeoutError:
                continue
            if header == 0xD0:
                if body or ping_deadline is None:
                    raise ProtocolError("Invalid or unsolicited PINGRESP")
                ping_deadline = None
                next_ping = time.monotonic() + 10
            else:
                self.incoming(header, body)

    def command(self, payload):
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.transport.send(packet(0x30, mqtt_string("cmd") + data))
        print(
            "Command written to TLS transport (QoS 0); execution is NOT confirmed. "
            "Check Matter or the robot.",
            file=sys.stderr,
        )


def discover(target, timeout):
    """Probe UDP 5678 with irobotmcs, accepting plain or length-prefixed JSON replies."""
    address = ipaddress.IPv4Address(target)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(b"irobotmcs", (str(address), 5678))
        deadline = time.monotonic() + timeout
        found = set()
        while time.monotonic() < deadline:
            sock.settimeout(max(0.001, deadline - time.monotonic()))
            try:
                data, peer = sock.recvfrom(65535)
            except socket.timeout:
                break
            if peer[0] in found:
                continue
            try:
                if not data.startswith(b"{"):
                    if len(data) < 2:
                        raise ValueError("truncated discovery length")
                    length = struct.unpack("!H", data[:2])[0]
                    if len(data) != length + 2:
                        raise ValueError("invalid discovery length")
                    data = data[2:]
                info = json.loads(data)
                if not isinstance(info, dict):
                    raise ValueError("discovery response is not an object")
            except (ValueError, UnicodeError) as exc:
                print(f"Invalid discovery response from {peer[0]}: {exc}", file=sys.stderr)
                continue
            found.add(peer[0])
            print(json.dumps({"ip": peer[0], "info": info}), flush=True)
        if not found:
            raise TimeoutError("No discovery responses; try --target with the robot's IPv4 address")


def positive_seconds(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("Must be a finite positive number")
    return number


def main():
    """Keep dry-run ahead of credentials/network setup; actual commands require --execute."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    discovery = sub.add_parser("discover", help="Local UDP identification; no cleaning command")
    discovery.add_argument("--target", default="255.255.255.255")
    discovery.add_argument("--timeout", type=positive_seconds, default=5)
    for action in ("listen", "start", "stop", "dock", "pause", "resume"):
        child = sub.add_parser(action)
        child.add_argument("--ip", default=os.environ.get("ROOMBA_IP"))
        child.add_argument("--blid", default=os.environ.get("ROOMBA_BLID"))
        child.add_argument("--port", type=int, default=8883)
        child.add_argument("--timeout", type=positive_seconds, default=10)
        child.add_argument(
            "--password-file", type=Path, help="Otherwise read ROOMBA_PASSWORD or prompt"
        )
        trust = child.add_mutually_exclusive_group()
        trust.add_argument(
            "--ca-file", type=Path, help="Trusted PEM certificate/CA; verifies IP SAN"
        )
        trust.add_argument(
            "--insecure", action="store_true", help="Explicitly disable server authentication"
        )
        child.add_argument(
            "--duration", type=positive_seconds, default=60 if action == "listen" else 2
        )
        child.add_argument(
            "--topic", action="append", help="Subscription filter; repeat to select topics"
        )
        if action != "listen":
            child.add_argument(
                "--execute", action="store_true", help="Send command; otherwise dry-run"
            )
        if action == "start":
            child.add_argument("--mop", action="store_true")
            child.add_argument("--wetness", type=int, choices=(1, 2, 3))
    args = parser.parse_args()
    if args.action == "discover":
        discover(args.target, args.timeout)
        return
    payload = None
    if args.action != "listen":
        if args.action == "start" and args.wetness is not None and not args.mop:
            parser.error("--wetness requires --mop")
        payload = command_payload(
            args.action,
            getattr(args, "mop", False),
            getattr(args, "wetness", None) or 2,
        )
        if not args.execute:
            print(json.dumps({"dry_run": True, "topic": "cmd", "payload": payload}, indent=2))
            return
    if not args.ip or not args.blid:
        parser.error("Set --ip/--blid or ROOMBA_IP/ROOMBA_BLID")
    ipaddress.ip_address(args.ip)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if not args.ca_file and not args.insecure:
        parser.error("Use --ca-file or explicitly acknowledge --insecure")
    if args.password_file:
        password = args.password_file.read_text().rstrip("\r\n")
    else:
        password = os.environ.get("ROOMBA_PASSWORD")
        if password is None:
            import getpass

            password = getpass.getpass("Robot MQTT password (not account password): ")
    mqtt_string(args.blid)
    mqtt_string(password)
    if args.insecure:
        print(
            "WARNING: robot TLS certificate is NOT authenticated; use only on a trusted LAN.",
            file=sys.stderr,
        )
    # Discovery also wakes the tested V4 MQTT listener before TLS connection.
    address = ipaddress.ip_address(args.ip)
    family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
    with socket.socket(family, socket.SOCK_DGRAM) as wake:
        wake.sendto(b"irobotmcs", (args.ip, 5678))
    time.sleep(0.5)
    transport = TLS(args.ip, args.port, args.timeout, args.ca_file, args.insecure)
    try:
        client = MQTT(transport)
        client.connect(args.blid, password)
        print("Robot accepted MQTT login.", file=sys.stderr)
        for topic in args.topic or ["#", "$aws/#"]:
            client.subscribe(topic)
        if payload is not None:
            client.command(payload)
        client.listen(args.duration)
        transport.send(packet(0xE0))
        print(
            "Observation window ended; absence of messages is not proof of no telemetry.",
            file=sys.stderr,
        )
    finally:
        transport.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        sys.exit(130)
    except (OSError, ValueError, ProtocolError, subprocess.SubprocessError) as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
