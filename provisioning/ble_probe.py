#!/usr/bin/env python3
"""Read-only BLE probe for the iRobot V4 provisioning GATT service.

Connects, lists every characteristic with its properties, and reads the
readable ones except explicitly excluded secrets/bulk data. Never writes.
Install requirements-provisioning on a host with Bluetooth permissions.
Read-only does not mean public: output can still identify a home network/robot.
For actual authorized Wi-Fi/cloud setup use ble_provision, not this probe.
"""

import argparse
import asyncio
import json
import sys

from bleak import BleakClient, BleakScanner

SERVICE = "c74edd21-763c-4e54-85a8-43bb75035d75"
NAMES = {
    0x01: "wlcfg_ssid",
    0x02: "wlcfg_bssid",
    0x03: "wlcfg_channel",
    0x04: "wlcfg_password(W)",
    0x05: "wlcfg_ip",
    0x06: "wlcfg_subnet",
    0x07: "wlcfg_gateway",
    0x08: "wlcfg_dns1",
    0x09: "wlcfg_dns2",
    0x0A: "robot_password",
    0x0B: "wifi_ctl",
    0x0C: "wifi_status",
    0x0D: "robot_ip",
    0x0E: "rc_time",
    0x0F: "rc_country",
    0x10: "rc_cloud_env",
    0x11: "rc_ntp_hosts",
    0x12: "rc_service_discovery",
    0x13: "rc_robot_name",
    0x14: "disc_version",
    0x15: "disc_hostname",
    0x16: "disc_sw_ver",
    0x17: "disc_cap_start",
    0x18: "disc_cap_end",
    0x19: "disc_mac",
    0x1A: "disc_protocol",
    0x1B: "wifi_error_desc",
    0x1C: "log_file_size",
    0x1D: "log_file_data",
    0x1E: "log_truncate_size",
    0x1F: "wifi_frequency",
    0x20: "ssid_list",
    0x21: "ssid_list_size",
}
# Never read these: secrets or bulk data.
SKIP = {0x0A, 0x1D, 0x04}


def short_id(uuid):
    u = uuid.lower()
    if u.startswith("c74e") and u.endswith("-763c-4e54-85a8-43bb75035d75"):
        return int(u[4:8], 16)
    return None


def render(data):
    try:
        text = data.decode("utf-8")
        if text.isprintable():
            return {"text": text}
    except UnicodeDecodeError:
        pass
    return {"hex": data.hex()}


async def find_address(name_filter, timeout):
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    for addr, (dev, adv) in found.items():
        if SERVICE in [u.lower() for u in adv.service_uuids]:
            if not name_filter or name_filter in (dev.name or ""):
                return addr
    return None


async def main(args):
    addr = args.address or await find_address(args.name, args.scan_time)
    if not addr:
        print("Robot not advertising the provisioning service", file=sys.stderr)
        return 1
    print(json.dumps({"event": "connecting", "address": addr}), flush=True)
    async with BleakClient(addr, timeout=20) as client:
        for svc in client.services:
            if svc.uuid.lower() != SERVICE and not args.all_services:
                continue
            print(json.dumps({"service": svc.uuid}), flush=True)
            for ch in svc.characteristics:
                sid = short_id(ch.uuid)
                row = {"uuid": ch.uuid, "name": NAMES.get(sid, "?"), "props": ch.properties}
                if "read" in ch.properties and sid not in SKIP:
                    try:
                        row["value"] = render(bytes(await client.read_gatt_char(ch)))
                    except Exception as exc:  # noqa: BLE001 - diagnostic tool
                        row["read_error"] = str(exc)
                print(json.dumps(row), flush=True)
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--address", help="BLE address (default: auto-scan)")
    p.add_argument("--name", default="", help="filter advertised name, e.g. N186020")
    p.add_argument("--scan-time", type=float, default=10)
    p.add_argument("--all-services", action="store_true")
    sys.exit(asyncio.run(main(p.parse_args())))
