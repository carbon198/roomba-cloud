#!/usr/bin/env python3
"""Connect a V4 Roomba to your emulator over Bluetooth.

This writes Wi-Fi and cloud settings
after robot authorization. Wi-Fi passwords are prompted privately. Use --debug for BLE
diagnostics. Provisioning can interrupt existing cloud and Matter operation.

Run on a host with a local BLE adapter, not inside the server containers. Linux
uses BlueZ/adapter selection; macOS/Windows use Bleak's system adapter (macOS
addresses are UUIDs, not MACs). Those backends are supported but only Linux has
been physically tested. Grant terminal/Python Bluetooth permissions; install
requirements-provisioning including tzdata on hosts without an IANA timezone DB.

This is NOT a dry run. Keep Matter as a fallback and a way to reprovision; do not
factory reset. Pass a DNS hostname, not URL/IP: discovery becomes
https://DOMAIN/v1/robot/discover. DNS/certificate trust must already be correct.
No writes occur before status 100 authorization; status 104 requests a physical
Power press after the beep. An already authorized robot needs no extra press.
Finish acknowledgement confirms the BLE/Wi-Fi sequence, not cloud registration;
check the authenticated dashboard separately. The generated robot password is
shown once locally and must remain private. Third-party BLE debug logging stays
off because raw GATT payloads can disclose SSID/password credentials.
"""

import argparse
import asyncio
import getpass
import ipaddress
import json
import logging
import random
import re
import string
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

from provisioning.ble_probe import SERVICE

LOGGER = logging.getLogger(__name__)


def debug(event, **fields):
    LOGGER.debug("%s", json.dumps({"event": event, **fields}))


def domain_name(value):
    try:
        domain = value.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise argparse.ArgumentTypeError("Enter a DNS hostname, not a URL.") from exc
    if (
        len(domain) > 253
        or "." not in domain
        or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in domain.split(".")
        )
    ):
        raise argparse.ArgumentTypeError(
            "Enter a DNS hostname such as roomba.example.com, without https:// or a path."
        )
    try:
        ipaddress.ip_address(domain)
    except ValueError:
        return domain
    raise argparse.ArgumentTypeError("Use the emulator's DNS hostname, not an IP address.")


def choose(items, description):
    for number, (_, label) in enumerate(items, 1):
        print(f"  {number}. {label}", flush=True)
    while True:
        answer = input(f"Choose {description} [1-{len(items)}]: ").strip()
        if answer.isdecimal() and 1 <= int(answer) <= len(items):
            return items[int(answer) - 1][0]
        print("Please enter one of the listed numbers.", flush=True)


def country_code(value):
    value = value.upper()
    if not re.fullmatch(r"[A-Z]{2}", value):
        raise argparse.ArgumentTypeError("Country must be a two-letter code such as US.")
    return value


async def select_adapter():
    """Choose a powered BLE-central Linux adapter or defer to the supported OS backend."""
    if sys.platform != "linux":
        if sys.platform not in ("darwin", "win32"):
            raise BleakError("Supported Bluetooth platforms are Linux, macOS and Windows.")
        print(
            "Using the system Bluetooth adapter. This platform does not offer adapter selection "
            "through Bleak.",
            flush=True,
        )
        return None
    from dbus_fast import BusType, Message, MessageType
    from dbus_fast.aio import MessageBus

    bus = await asyncio.wait_for(MessageBus(bus_type=BusType.SYSTEM).connect(), 10)
    try:
        reply = await asyncio.wait_for(
            bus.call(
                Message(
                    destination="org.bluez",
                    path="/",
                    interface="org.freedesktop.DBus.ObjectManager",
                    member="GetManagedObjects",
                )
            ),
            10,
        )
        if reply.message_type == MessageType.ERROR:
            raise BleakError(
                "Cannot query Bluetooth adapters. Check that BlueZ is installed and running."
            )
        adapters = []
        for path, interfaces in reply.body[0].items():
            properties = interfaces.get("org.bluez.Adapter1")
            if properties is None:
                continue
            powered = properties.get("Powered")
            roles = properties.get("Roles")
            if not powered or not powered.value or (roles and "central" not in roles.value):
                continue
            identifier = path.rsplit("/", 1)[-1]
            alias = properties.get("Alias")
            address = properties.get("Address")
            label = f"{identifier} - {alias.value if alias else 'Bluetooth'}"
            if address:
                label += f" ({address.value})"
            adapters.append((identifier, label))
    finally:
        bus.disconnect()
    if not adapters:
        raise BleakError(
            "No powered BLE adapter is available. Connect an adapter, turn Bluetooth on, "
            "and check that it is not blocked by airplane mode."
        )
    adapters.sort()
    if len(adapters) > 1:
        print("Available Bluetooth adapters:", flush=True)
        return choose(adapters, "a Bluetooth adapter")
    print(f"Using Bluetooth adapter: {adapters[0][1]}", flush=True)
    return adapters[0][0]


async def select_robot(a, adapter):
    print(f"Searching for Roombas ({a.scan_time:g} seconds)...", flush=True)
    options = {"bluez": {"adapter": adapter}} if adapter else {}
    found = await BleakScanner.discover(
        timeout=a.scan_time, return_adv=True, service_uuids=[SERVICE], **options
    )
    robots = []
    for device, advertisement in found.values():
        if SERVICE not in [value.lower() for value in advertisement.service_uuids]:
            continue
        name = advertisement.local_name or device.name or "Unnamed Roomba"
        if a.address and device.address.lower() != a.address.lower():
            continue
        if a.name and a.name.lower() not in name.lower():
            continue
        robots.append((device, f"{name} ({device.address})"))
    if not robots:
        raise BleakError(
            "No Roombas found. Check provisioning mode, Bluetooth permissions, "
            "and distance from the robot, then retry."
        )
    robots.sort(key=lambda item: item[1])
    print("Discovered Roombas:", flush=True)
    return choose(robots, "a robot")


SUFFIX = "-763c-4e54-85a8-43bb75035d75"


def uuid(n):
    return f"c74e{n:04x}{SUFFIX}"


U = {
    "ssid": uuid(0x01),
    "password": uuid(0x04),
    "robot_password": uuid(0x0A),
    "wifi_ctl": uuid(0x0B),
    "wifi_status": uuid(0x0C),
    "robot_ip": uuid(0x0D),
    "time": uuid(0x0E),
    "country": uuid(0x0F),
    "cloud_env": uuid(0x10),
    "sdisc": uuid(0x12),
    "name": uuid(0x13),
    "hostname": uuid(0x15),
    "wifi_freq": uuid(0x1F),
    "wifi_error": uuid(0x1B),
}
APPLY, FINISH = 0x65, 0x66
STATUS_NAMES = {
    0: "WifiUnconfigured",
    1: "WifiConnected",
    2: "WifiNetworkError",
    3: "WifiAuthenticationError",
    4: "WifiDHCPError",
    5: "WifiLinkLostError",
    6: "WifiConnectError",
    8: "WifiConnecting",
    9: "WifiConfigurationError",
    10: "WifiSystemError",
    99: "IpDoneReceived",
    100: "UserConfirmation",
    103: "ProvisioningConfirmationError",
    104: "ConfirmationTimeoutStarted",
    108: "SsidListAvailable",
    109: "SsidListTimeoutError",
}
DISCOVERY = (0x14, 0x15, 0x16, 0x17, 0x18, 0x19, 0x1A)
SUBSCRIPTIONS = (0x0C, 0x1C, 0x1D)


@asynccontextmanager
async def provisioning_cleanup(client, subscribed):
    """Unsubscribe status when possible and disconnect even if cleanup itself fails."""
    try:
        yield
    finally:
        if client.is_connected:
            try:
                if U["wifi_status"] in subscribed:
                    await client.stop_notify(U["wifi_status"])
                    debug("status_unsubscribed")
            finally:
                await client.disconnect()
                debug("disconnected")


async def setup_provisioning_session(client, status_callback, subscribed=None, log_callback=None):
    """Use the discovery/indication sequence verified with the iPhone app."""

    def discard_log(_, data):
        pass

    for number in DISCOVERY:
        data = await client.read_gatt_char(uuid(number))
        debug("discovery_read", characteristic=number, bytes=len(data))
    for number in SUBSCRIPTIONS:
        callback = status_callback if number == 0x0C else (log_callback or discard_log)
        await client.start_notify(uuid(number), callback)
        if subscribed is not None:
            subscribed.append(uuid(number))
        debug("subscribed", characteristic=number)


def status_fields(data):
    code = data[0] if len(data) == 1 else None
    return {"hex": data.hex(), "code": code, "status": STATUS_NAMES.get(code, "Unknown")}


async def read_diagnostics(client):
    status = bytes(await client.read_gatt_char(U["wifi_status"]))
    error = bytes(await client.read_gatt_char(U["wifi_error"]))
    ip = bytes(await client.read_gatt_char(U["robot_ip"])).decode("utf-8")
    return {**status_fields(status), "wifi_error_hex": error.hex(), "ip": ip}


async def await_user_confirmation(status_queue, timeout):
    """Wait for firmware authorization; never mistake timeout-started=104 for consent."""
    print("Waiting for the robot to authorize setup...", flush=True)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            data = await asyncio.wait_for(status_queue.get(), max(0, deadline - time.monotonic()))
        except TimeoutError:
            break
        fields = status_fields(data)
        debug("awaiting_confirmation", **fields)
        if fields["code"] == 104:
            print(
                "Next step: after the robot beeps, press its Power button once to authorize setup.",
                flush=True,
            )
        if fields["code"] == 100:
            print("Setup authorized. Continuing with configuration.", flush=True)
            return True
        if fields["code"] == 103:
            print("Robot rejected provisioning confirmation; no settings written.", file=sys.stderr)
            return False
    print(
        "Authorization timed out; no settings written. Re-enter provisioning mode "
        "and retry. Only press Power when instructed.",
        file=sys.stderr,
    )
    return False


async def finish_when_connected(client, status_queue, timeout):
    """Require fresh WifiConnected before reading a valid IP and writing FINISH.

    An old IP can remain populated while the new join is pending. Failure/timeout
    must not send FINISH and claim successful provisioning.
    """

    async def wait_and_finish():
        while True:
            fields = status_fields(await status_queue.get())
            if fields["code"] in (2, 3, 4, 5, 6, 9, 10, 103):
                print(
                    f"Wi-Fi setup failed: {fields['status']}. Check your Wi-Fi name/password "
                    "and signal, then retry.",
                    file=sys.stderr,
                )
                if LOGGER.isEnabledFor(logging.DEBUG):
                    debug("wifi_failed", **fields, diagnostics=await read_diagnostics(client))
                return False
            if fields["code"] == 1:
                break
        while True:
            ip = bytes(await client.read_gatt_char(U["robot_ip"])).decode("utf-8")
            if ip:
                address = ipaddress.IPv4Address(ip)
                if address.is_unspecified:
                    raise ValueError("Robot reported an unspecified IP address")
                break
            await asyncio.sleep(1)
        await client.read_gatt_char(U["wifi_freq"])
        print(f"Robot connected to Wi-Fi: {ip}", flush=True)
        await client.write_gatt_char(U["wifi_ctl"], bytes([FINISH]), response=True)
        debug("finished", meaning="finish write acknowledged; cloud setup unverified")
        return True

    try:
        return await asyncio.wait_for(wait_and_finish(), timeout)
    except TimeoutError:
        print("Timed out waiting for WifiConnected and an IP; not sending finish.", file=sys.stderr)
        return False


def raw_offset_minutes(tz_name):
    """Standard (non-DST) UTC offset, like Java's TimeZone.getRawOffset()."""
    tz = ZoneInfo(tz_name)
    year = datetime.now().year
    offsets = [datetime(year, m, 1, tzinfo=tz).utcoffset() for m in (1, 7)]
    return int(min(offsets).total_seconds() // 60)


def time_payload(tz_name, now=None):
    now = time.time() if now is None else now
    body = {
        "timezone": tz_name,
        "utctime": int(now),
        "localtimeoffset": raw_offset_minutes(tz_name),
    }
    return json.dumps(body, separators=(",", ":")).encode()


def sdisc_payload(url, robot_id, sku, country):
    """Encode the firmware's JSON member fragment, not a complete standalone JSON object."""
    query = urlencode({"robot_id": robot_id, "sku": sku, "country_code": country})
    return f'"sdiscUrl":"{url}?{query}"'.encode()


def robot_password(now=None):
    ts = str(int((time.time() if now is None else now)))[:12]
    rnd = random.SystemRandom()
    chars = string.ascii_letters + string.digits
    return ":1:" + ts + ":" + "".join(rnd.choice(chars) for _ in range(16))


def build_writes(a, robot_id, wifi_password, robot_pw):
    """Mirror the observed app order and wire encodings, ending with APPLY.

    SSID/password are hex-encoded UTF-8; identity/cloud fields use member
    fragments, not one generic JSON serializer. Do not reorder based on GATT UUID
    order. Setup authorization precedes these writes; FINISH is a later step.
    """
    return [
        ("time", U["time"], time_payload(a.timezone)),
        ("country", U["country"], f'"country": "{a.country}"'.encode()),
        ("cloud_env", U["cloud_env"], f'"cloudEnv": "{a.cloud_env}"'.encode()),
        (
            "sdisc",
            U["sdisc"],
            sdisc_payload(f"https://{a.domain}/v1/robot/discover", robot_id, a.sku, a.country),
        ),
        ("name", U["name"], f'"name": {json.dumps(a.robot_name)}'.encode()),
        ("ssid", U["ssid"], a.ssid.encode().hex().encode()),
        ("password", U["password"], wifi_password.encode().hex().encode()),
        ("robot_password", U["robot_password"], robot_pw.encode()),
        ("wifi_ctl", U["wifi_ctl"], bytes([APPLY])),
    ]


SECRET = {"password", "robot_password", "ssid"}


async def main(a):
    """Authorize, identify, write once, then wait for Wi-Fi/finish acknowledgement.

    Flush pre-APPLY statuses so old notifications cannot complete the new join.
    Failed/partial provisioning may need reprovisioning; neither disconnect nor
    a printed dashboard link independently proves cloud registration.
    """
    print("Roomba emulator setup", flush=True)
    print(
        "This changes the robot's Wi-Fi and cloud settings. Keep your emulator running.", flush=True
    )
    adapter = await select_adapter()
    device = await select_robot(a, adapter)
    wifi_pw = getpass.getpass(f"Password for Wi-Fi '{a.ssid}' (hidden): ")
    if not wifi_pw:
        print("Empty Wi-Fi password; aborting.", file=sys.stderr)
        return 1
    robot_pw = robot_password()
    subscribed = []
    print(f"Connecting to {device.name or 'Roomba'} ({device.address})...", flush=True)
    async with BleakClient(device, timeout=20) as client, provisioning_cleanup(client, subscribed):
        print("Connected to the robot.", flush=True)
        status_queue = asyncio.Queue()

        def on_status(_, data):
            debug("wifi_status", **status_fields(data))
            status_queue.put_nowait(bytes(data))

        await setup_provisioning_session(client, on_status, subscribed)
        if not await await_user_confirmation(status_queue, a.confirm_wait):
            return 2
        host = bytes(await client.read_gatt_char(U["hostname"])).decode("utf-8")
        robot_id = host.split("iRobot-")[-1].strip('"')
        if not re.fullmatch(r"[0-9A-Fa-f]{32}", robot_id):
            raise ValueError("Robot reported an invalid identity; no settings written.")
        writes = build_writes(a, robot_id, wifi_pw, robot_pw)
        for name, _, data in writes:
            shown = "<hidden>" if name in SECRET else data.decode(errors="replace")
            debug("planned_write", name=name, bytes=len(data), value=shown)
        if LOGGER.isEnabledFor(logging.DEBUG):
            debug("before_apply", **await read_diagnostics(client))
        print("Writing Wi-Fi and emulator settings...", flush=True)
        for name, char, data in writes:
            if name == "wifi_ctl":
                while not status_queue.empty():
                    status_queue.get_nowait()
            await client.write_gatt_char(char, data, response=True)
            debug("written", name=name)

        print(f"ROBOT PASSWORD (save this): {robot_pw}", flush=True)
        print("Waiting for the robot to join Wi-Fi...", flush=True)
        try:
            finished = await finish_when_connected(client, status_queue, a.wait)
        except (BleakError, OSError, UnicodeDecodeError, ValueError) as exc:
            print(
                f"Unable to finish provisioning: {exc}. Setup may be incomplete.", file=sys.stderr
            )
            LOGGER.debug("Provisioning failed", exc_info=True)
            return 2
    if finished:
        print(
            "Setup complete. The robot should now be registered with your emulator.\n"
            f"Open https://{a.domain}/ to check its status (customer token required).",
            flush=True,
        )
    return 0 if finished else 2


def parse_arguments(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--ssid", required=True)
    p.add_argument(
        "--domain",
        required=True,
        type=domain_name,
        help="emulator hostname, e.g. roomba.example.com (not a URL)",
    )
    p.add_argument("--country", default="US", type=country_code)
    p.add_argument("--timezone", default="America/New_York")
    p.add_argument("--cloud-env", default="prod")
    p.add_argument("--robot-name", default="Roomba")
    p.add_argument("--sku", default="N186020")
    p.add_argument("--address")
    p.add_argument("--name", default="", help="optional advertised-name filter")
    p.add_argument("--scan-time", type=float, default=10, help="Bluetooth scan duration in seconds")
    p.add_argument(
        "--wait", type=float, default=60, help="seconds to wait for WifiConnected and a valid IP"
    )
    p.add_argument(
        "--confirm-wait",
        type=float,
        default=90,
        help="seconds to wait for the physical Power-button confirmation",
    )
    p.add_argument("--debug", action="store_true", help="show BLE diagnostics and debug logs")
    args = p.parse_args(argv)
    if any(not 0 < value <= 3600 for value in (args.scan_time, args.wait, args.confirm_wait)):
        p.error("Scan and wait durations must be greater than zero and at most 3600 seconds.")
    if not args.ssid or len(args.ssid.encode()) > 32:
        p.error("Wi-Fi SSID must contain 1-32 bytes.")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", args.cloud_env):
        p.error("Invalid cloud environment.")
    try:
        raw_offset_minutes(args.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        p.error(
            f"Unknown timezone '{args.timezone}'. Install provisioning requirements (including tzdata)."
        )
    return args


def cli():
    args = parse_arguments()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    LOGGER.setLevel(logging.DEBUG if args.debug else logging.WARNING)
    try:
        return asyncio.run(main(args))
    except TimeoutError:
        print("Bluetooth operation timed out. Check the adapter and retry.", file=sys.stderr)
        LOGGER.debug("Bluetooth timed out", exc_info=True)
        return 2
    except (BleakError, OSError, ValueError, EOFError) as exc:
        print(f"Setup failed: {exc}", file=sys.stderr)
        if not args.debug:
            print(
                "Check Bluetooth is enabled and this terminal has Bluetooth permission. "
                "Use --debug for diagnostics.",
                file=sys.stderr,
            )
        LOGGER.debug("Setup failed", exc_info=True)
        return 2
    except KeyboardInterrupt:
        print(
            "\nSetup cancelled. If writing had begun, the robot may need reprovisioning.",
            file=sys.stderr,
        )
        return 130


if __name__ == "__main__":
    sys.exit(cli())
