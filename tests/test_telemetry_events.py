import contextlib
import io
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from roomba_cloud.telemetry_events import TelemetryEvents, TelemetryNotifier, event_path


A = "a" * 32
B = "b" * 32


class TelemetryEventsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = Path(self.directory.name) / "shadows.db"

    def test_delivery_is_scoped_and_subscriptions_are_removed(self):
        with TelemetryEvents(self.database) as events:
            self.assertEqual(events.path.stat().st_mode & 0o777, 0o600)
            with events.subscribe(A) as first, events.subscribe(A) as second:
                with events.subscribe(B) as unrelated:
                    TelemetryNotifier(self.database).notify(A)
                    self.assertTrue(first.wait(2))
                    self.assertTrue(second.wait(2))
                    self.assertFalse(unrelated.is_set())
                self.assertNotIn(B, events._subscribers)
            self.assertFalse(events._subscribers)
        self.assertTrue(events.stopped)
        self.assertFalse(events._thread.is_alive())
        self.assertFalse(events.path.exists())

    def test_shutdown_wakes_waiters(self):
        events = TelemetryEvents(self.database)
        with events.subscribe(A) as waiting:
            with events:
                self.assertFalse(waiting.is_set())
            self.assertTrue(waiting.is_set())

    def test_invalid_datagrams_do_not_wake_subscribers(self):
        with TelemetryEvents(self.database) as events:
            with events.subscribe(A) as target, events.subscribe(B) as sentinel:
                with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sender:
                    for payload in (b"", b"not-a-robot", A.encode() + b"x"):
                        sender.sendto(payload, str(events.path))
                    sender.sendto(B.encode(), str(events.path))
                self.assertTrue(sentinel.wait(2))
                self.assertFalse(target.is_set())

    def test_stale_socket_recovered_but_active_listener_not_replaced(self):
        path = event_path(self.database)
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as stale:
            stale.bind(str(path))
            path.chmod(0o600)
        with TelemetryEvents(self.database) as events:
            with self.assertRaisesRegex(ValueError, "already has a listener"):
                TelemetryEvents(self.database)
            with events.subscribe(A) as waiting:
                TelemetryNotifier(self.database).notify(A)
                self.assertTrue(waiting.wait(2))

    def test_unsafe_paths_are_preserved_and_rejected(self):
        path = event_path(self.database)
        path.write_text("not a socket")
        with self.assertRaisesRegex(ValueError, "private socket"):
            TelemetryEvents(self.database)
        self.assertEqual(path.read_text(), "not a socket")
        path.unlink()
        Path(self.directory.name).chmod(0o755)
        with self.assertRaisesRegex(ValueError, "directory must be private"):
            TelemetryEvents(self.database)

    def test_shutdown_does_not_unlink_replacement(self):
        with TelemetryEvents(self.database) as events:
            events.path.unlink()
            events.path.write_text("replacement")
        self.assertEqual(events.path.read_text(), "replacement")

    def test_listener_error_wakes_subscribers_and_is_reported(self):
        output = io.StringIO()
        events = TelemetryEvents(self.database)
        with patch.object(events, "_socket", wraps=events._socket) as sock:
            sock.recv.side_effect = OSError("private details must not be logged")
            with events.subscribe(A) as waiting, contextlib.redirect_stdout(output):
                with events:
                    self.assertTrue(waiting.wait(2))
                    self.assertTrue(events.stopped)
        self.assertIn("telemetry_listener_failed", output.getvalue())
        self.assertIn("OSError", output.getvalue())
        self.assertNotIn("private details", output.getvalue())

    def test_notifier_failure_is_rate_limited_and_invalid_ids_are_ignored(self):
        output = io.StringIO()
        notifier = TelemetryNotifier(self.database)
        with (
            contextlib.redirect_stdout(output),
            patch("roomba_cloud.telemetry_events.time.monotonic") as clock,
        ):
            clock.return_value = 100
            notifier.notify("invalid")
            self.assertEqual(output.getvalue(), "")
            notifier.notify(A)
            notifier.notify(A)
            clock.return_value = 131
            notifier.notify(A)
        self.assertEqual(output.getvalue().count("telemetry_notification_failed"), 2)
