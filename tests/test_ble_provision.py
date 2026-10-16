import argparse
import asyncio
import contextlib
import io
import logging
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from bleak.exc import BleakError

import provisioning.ble_provision as ble


ROBOT = "A" * 32


class ProvisioningArgumentsTests(unittest.TestCase):
    def args(self, *flags):
        return ble.parse_arguments(
            ["--ssid", "Private Wi-Fi", "--domain", "Roomba.Example.COM", *flags]
        )

    def test_domain_and_fixed_discovery_url(self):
        args = self.args()
        self.assertEqual(args.domain, "roomba.example.com")
        self.assertFalse(hasattr(args, "execute"))
        self.assertFalse(args.debug)
        self.assertEqual(args.name, "")
        writes = ble.build_writes(args, ROBOT, "wifi-secret", "robot-secret")
        self.assertEqual(
            [row[0] for row in writes],
            [
                "time",
                "country",
                "cloud_env",
                "sdisc",
                "name",
                "ssid",
                "password",
                "robot_password",
                "wifi_ctl",
            ],
        )
        self.assertIn(
            b"https://roomba.example.com/v1/robot/discover?robot_id=" + ROBOT.encode(), writes[3][2]
        )
        self.assertEqual(writes[-1][2], b"\x65")
        for domain in (
            "https://roomba.example.com",
            "roomba.example.com/path",
            "roomba.example.com:8443",
            "127.0.0.1",
            "bad..com",
            "-bad.com",
            "localhost",
        ):
            with self.subTest(domain=domain), self.assertRaises(argparse.ArgumentTypeError):
                ble.domain_name(domain)
        self.assertEqual(ble.domain_name("roomba.example.com."), "roomba.example.com")

    def test_invalid_inputs_and_removed_url_are_rejected(self):
        for flags in (
            ["--url", "https://example.com"],
            ["--execute"],
            ["--wait", "0"],
            ["--scan-time", "nan"],
            ["--country", "USA"],
            ["--timezone", "Not/AZone"],
            ["--cloud-env", 'bad"'],
            ["--ssid", "x" * 33],
        ):
            with (
                self.subTest(flags=flags),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                self.args(*flags)

    def test_numbered_selection_retries_invalid_input(self):
        with (
            patch("builtins.input", side_effect=["", "zero", "0", "3", "2"]),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(ble.choose([("a", "First"), ("b", "Second")], "a robot"), "b")
        self.assertIn("1. First", output.getvalue())
        self.assertIn("2. Second", output.getvalue())

    def test_debug_events_are_opt_in(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        previous = ble.LOGGER.level
        ble.LOGGER.addHandler(handler)
        try:
            ble.LOGGER.setLevel(logging.WARNING)
            ble.debug("wifi_status", code=104)
            self.assertEqual(stream.getvalue(), "")
            ble.LOGGER.setLevel(logging.DEBUG)
            ble.debug("wifi_status", code=104)
            self.assertIn('"code": 104', stream.getvalue())
        finally:
            ble.LOGGER.removeHandler(handler)
            ble.LOGGER.setLevel(previous)

    def test_cli_adapter_failure_has_friendly_error_without_traceback(self):
        flags = ["ble_provision.py", "--ssid", "Wi-Fi", "--domain", "roomba.example.com"]
        with (
            patch.object(sys, "argv", flags),
            patch.object(
                ble,
                "main",
                new_callable=AsyncMock,
                side_effect=BleakError("No powered BLE adapter is available."),
            ),
            contextlib.redirect_stderr(io.StringIO()) as output,
        ):
            self.assertEqual(ble.cli(), 2)
        self.assertIn("No powered BLE adapter", output.getvalue())
        self.assertNotIn("Traceback", output.getvalue())


class ProvisioningFlowTests(unittest.IsolatedAsyncioTestCase):
    def args(self):
        flags = ["--ssid", "Private Wi-Fi", "--domain", "roomba.example.com"]
        return ble.parse_arguments(flags)

    async def test_confirmation_100_without_104_never_prompts_for_power(self):
        queue = asyncio.Queue()
        queue.put_nowait(bytes([100]))
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertTrue(await ble.await_user_confirmation(queue, 1))
        self.assertNotIn("Power", output.getvalue())
        self.assertNotIn('"event"', output.getvalue())

    async def test_confirmation_104_then_100_prompts_before_continuing(self):
        queue = asyncio.Queue()
        for code in (104, 100):
            queue.put_nowait(bytes([code]))
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertTrue(await ble.await_user_confirmation(queue, 1))
        text = output.getvalue()
        self.assertIn("Next step:", text)
        self.assertLess(text.index("press its Power"), text.index("Setup authorized"))

    async def test_rejection_and_timeout_do_not_authorize(self):
        for code in (103, None):
            queue = asyncio.Queue()
            if code:
                queue.put_nowait(bytes([code]))
            with (
                self.subTest(code=code),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertFalse(await ble.await_user_confirmation(queue, 0.001))

    @unittest.skipUnless(sys.platform == "linux", "Linux adapter enumeration uses D-Bus")
    async def test_linux_adapter_selection_and_no_powered_adapter(self):
        from dbus_fast import MessageType, Variant

        adapters = {
            "/org/bluez/hci0": {
                "org.bluez.Adapter1": {
                    "Powered": Variant("b", True),
                    "Alias": Variant("s", "Internal"),
                }
            },
            "/org/bluez/hci1": {
                "org.bluez.Adapter1": {
                    "Powered": Variant("b", True),
                    "Alias": Variant("s", "USB"),
                    "Roles": Variant("as", ["central"]),
                }
            },
            "/org/bluez/hci2": {"org.bluez.Adapter1": {"Powered": Variant("b", False)}},
        }
        bus = Mock()
        bus.connect = AsyncMock(return_value=bus)
        bus.call = AsyncMock(
            return_value=SimpleNamespace(message_type=MessageType.METHOD_RETURN, body=[adapters])
        )
        with (
            patch.object(ble.sys, "platform", "linux"),
            patch("dbus_fast.aio.MessageBus", return_value=bus),
            patch("builtins.input", return_value="2"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(await ble.select_adapter(), "hci1")
            bus.disconnect.assert_called_once()
            adapters.pop("/org/bluez/hci1")
            self.assertEqual(await ble.select_adapter(), "hci0")
            adapters.clear()
            with self.assertRaisesRegex(BleakError, "No powered BLE adapter"):
                await ble.select_adapter()
            bus.call.return_value = SimpleNamespace(message_type=MessageType.ERROR)
            with self.assertRaisesRegex(BleakError, "BlueZ"):
                await ble.select_adapter()

    async def test_windows_and_macos_use_system_adapter_and_surface_scan_errors(self):
        for platform in ("win32", "darwin"):
            with (
                self.subTest(platform=platform),
                patch.object(ble.sys, "platform", platform),
                contextlib.redirect_stdout(io.StringIO()),
                patch.object(
                    ble.BleakScanner,
                    "discover",
                    new_callable=AsyncMock,
                    side_effect=BleakError("No Bluetooth adapter found"),
                ),
            ):
                self.assertIsNone(await ble.select_adapter())
                with self.assertRaisesRegex(BleakError, "No Bluetooth adapter"):
                    await ble.select_robot(self.args(), None)

    async def test_scan_lists_robots_and_keeps_selected_device_adapter_details(self):
        devices = [
            SimpleNamespace(name=name, address=address)
            for name, address in (
                ("Roomba One", "AA:01"),
                ("Roomba Two", "AA:02"),
                ("Not a robot", "AA:03"),
            )
        ]
        found = {
            device.address: (
                device,
                SimpleNamespace(
                    local_name=device.name, service_uuids=[ble.SERVICE] if index < 2 else []
                ),
            )
            for index, device in enumerate(devices)
        }
        with (
            patch.object(
                ble.BleakScanner, "discover", new_callable=AsyncMock, return_value=found
            ) as scan,
            patch("builtins.input", return_value="2"),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            selected = await ble.select_robot(self.args(), "hci1")
        self.assertIs(selected, devices[1])
        self.assertIn("Roomba One", output.getvalue())
        self.assertNotIn("Not a robot", output.getvalue())
        self.assertEqual(scan.call_args.kwargs["bluez"], {"adapter": "hci1"})
        with (
            patch.object(ble.BleakScanner, "discover", new_callable=AsyncMock, return_value={}),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(BleakError, "No Roombas"),
        ):
            await ble.select_robot(self.args(), None)

    async def test_wifi_finish_waits_for_status_and_valid_ip(self):
        client = Mock()
        client.read_gatt_char = AsyncMock(side_effect=[b"", b"192.0.2.42", b"2.4"])
        client.write_gatt_char = AsyncMock()
        queue = asyncio.Queue()
        queue.put_nowait(b"\x08")
        queue.put_nowait(b"\x01")
        with (
            patch.object(ble.asyncio, "sleep", new_callable=AsyncMock),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertTrue(await ble.finish_when_connected(client, queue, 1))
        client.write_gatt_char.assert_awaited_once_with(ble.U["wifi_ctl"], b"\x66", response=True)
        client.write_gatt_char.reset_mock()
        queue.put_nowait(b"\x03")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(await ble.finish_when_connected(client, queue, 1))
        client.write_gatt_char.assert_not_awaited()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(await ble.finish_when_connected(client, queue, 0.001))
        client.write_gatt_char.assert_not_awaited()

    async def test_authorized_and_denied_provisioning_flows(self):
        for authorize in (True, False):
            with self.subTest(authorize=authorize):
                client = Mock()
                client.is_connected = True
                client.__aenter__ = AsyncMock(return_value=client)
                client.__aexit__ = AsyncMock(return_value=False)
                client.read_gatt_char = AsyncMock(return_value=f"iRobot-{ROBOT}".encode())
                client.write_gatt_char = AsyncMock()
                client.stop_notify = AsyncMock()
                client.disconnect = AsyncMock()
                device = SimpleNamespace(name="Roomba One", address="AA:01")
                with (
                    patch.object(
                        ble, "select_adapter", new_callable=AsyncMock, return_value="hci1"
                    ),
                    patch.object(ble, "select_robot", new_callable=AsyncMock, return_value=device),
                    patch.object(ble, "BleakClient", return_value=client) as factory,
                    patch.object(ble.getpass, "getpass", return_value="wifi-secret") as password,
                    patch.object(ble, "setup_provisioning_session", new_callable=AsyncMock),
                    patch.object(
                        ble,
                        "await_user_confirmation",
                        new_callable=AsyncMock,
                        return_value=authorize,
                    ),
                    patch.object(
                        ble, "finish_when_connected", new_callable=AsyncMock, return_value=True
                    ) as finish,
                    contextlib.redirect_stdout(io.StringIO()) as output,
                ):
                    result = await ble.main(self.args())
                text = output.getvalue()
                factory.assert_called_once_with(device, timeout=20)
                self.assertIn("Connecting to", text)
                self.assertNotIn("wifi-secret", text)
                self.assertNotIn('"event"', text)
                password.assert_called_once()
                if authorize:
                    self.assertEqual(result, 0)
                    self.assertEqual(client.write_gatt_char.await_count, 9)
                    self.assertIn("https://roomba.example.com/", text)
                    self.assertIn("should now be registered", text)
                    finish.assert_awaited_once()
                else:
                    client.write_gatt_char.assert_not_awaited()
                    finish.assert_not_awaited()
                    self.assertNotIn("should now be registered", text)
                client.disconnect.assert_awaited_once()

    async def test_notifications_and_cleanup_remain_quiet(self):
        client = Mock()
        client.is_connected = True
        client.read_gatt_char = AsyncMock(return_value=b"version")
        client.start_notify = AsyncMock()
        client.stop_notify = AsyncMock()
        client.disconnect = AsyncMock()
        subscribed = []
        callback = Mock()
        with contextlib.redirect_stdout(io.StringIO()) as output:
            async with ble.provisioning_cleanup(client, subscribed):
                await ble.setup_provisioning_session(client, callback, subscribed)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(client.read_gatt_char.await_count, len(ble.DISCOVERY))
        self.assertEqual(client.start_notify.await_count, len(ble.SUBSCRIPTIONS))
        self.assertEqual(
            client.start_notify.await_args_list[0].args, (ble.U["wifi_status"], callback)
        )
        client.stop_notify.assert_awaited_once_with(ble.U["wifi_status"])
        client.disconnect.assert_awaited_once()
