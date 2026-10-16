"""Private coalescing wakeups, NOT the authoritative telemetry/event store.

MQTT and uploads commit SQLite first, then send a robot-ID datagram to HTTPS.
No geometry, token or request body crosses this socket. Subscriber threading
Events coalesce bursts; there is no sequence/durable queue to replay. Notification
failure is rate-limited/logged and the HTTP stream's 15-second reconciliation
recovers missed changes and lease expiry. A sender must not block a robot on a
full/missing listener socket.
"""

import contextlib
import errno
from collections.abc import Iterator
from os import PathLike
from pathlib import Path
import re
import socket
import stat
import threading
import time
from types import TracebackType
from typing import Self


def event_path(database: str | PathLike[str]) -> Path:
    return Path(str(Path(database).resolve()) + ".events.sock")


class TelemetryNotifier:
    """Best-effort nonblocking sender; persistent state remains the source of truth."""

    def __init__(self, database: str | PathLike[str]) -> None:
        self.path = event_path(database)
        self._next_warning = 0.0

    def notify(self, robot_id: str) -> None:
        if not re.fullmatch(r"[0-9A-Fa-f]{32}", robot_id):
            return
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as conn:
                conn.setblocking(False)
                conn.sendto(robot_id.encode("ascii"), str(self.path))
        except OSError as exc:
            # Wakeups are advisory: the stream heartbeat reconciles missed events.
            if time.monotonic() >= self._next_warning:
                from roomba_cloud.cloud_http import log

                log(
                    "telemetry_notification_failed",
                    error_type=type(exc).__name__,
                    recovery="stream heartbeat reconciliation",
                )
                self._next_warning = time.monotonic() + 30


class TelemetryEvents:
    """Own one private socket/listener and wake streams on changes or shutdown.

    Refuse active or unsafe preexisting paths; recover only a stale refused
    socket. Cleanup checks inode identity before unlinking, leaving replacements
    alone. Subscribers must exit their context to avoid retaining thread Events.
    """

    def __init__(self, database: str | PathLike[str]) -> None:
        self.path = event_path(database)
        self._lock = threading.Lock()
        self._subscribers: dict[str, set[threading.Event]] = {}
        self._stopped = threading.Event()
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            if self.path.parent.stat().st_mode & 0o077:
                raise ValueError("Telemetry socket directory must be private (mode 0700)")
            if self.path.exists() or self.path.is_symlink():
                info = self.path.lstat()
                if not stat.S_ISSOCK(info.st_mode) or info.st_mode & 0o077:
                    raise ValueError("Existing telemetry socket must be a private socket")
                with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as probe:
                    try:
                        probe.connect(str(self.path))
                    except OSError as exc:
                        if exc.errno != errno.ECONNREFUSED:
                            raise
                    else:
                        raise ValueError("Telemetry socket already has a listener")
                self.path.unlink()
            self._socket.bind(str(self.path))
            self._identity = self.path.stat().st_ino
            self.path.chmod(0o600)
            self._socket.settimeout(0.5)
        except (OSError, ValueError):
            self._socket.close()
            raise
        self._thread = threading.Thread(target=self._listen, daemon=True)

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._stopped.set()
        with self._lock:
            for subscribers in self._subscribers.values():
                for event in subscribers:
                    event.set()
        self._thread.join()
        self._socket.close()
        if self.path.exists() and self.path.stat().st_ino == self._identity:
            self.path.unlink()

    @property
    def stopped(self) -> bool:
        return self._stopped.is_set()

    @contextlib.contextmanager
    def subscribe(self, robot_id: str) -> Iterator[threading.Event]:
        """Register before reading a snapshot; always remove the subscriber on exit."""
        event = threading.Event()
        with self._lock:
            self._subscribers.setdefault(robot_id, set()).add(event)
        try:
            yield event
        finally:
            with self._lock:
                subscribers = self._subscribers[robot_id]
                subscribers.remove(event)
                if not subscribers:
                    del self._subscribers[robot_id]

    def _listen(self) -> None:
        while not self.stopped:
            try:
                raw = self._socket.recv(33)
            except TimeoutError:
                continue
            except OSError as exc:
                from roomba_cloud.cloud_http import log

                log("telemetry_listener_failed", error_type=type(exc).__name__)
                self._stopped.set()
                with self._lock:
                    for subscribers in self._subscribers.values():
                        for event in subscribers:
                            event.set()
                return
            if not re.fullmatch(rb"[0-9A-Fa-f]{32}", raw):
                continue
            with self._lock:
                for event in self._subscribers.get(raw.decode("ascii"), ()):
                    event.set()
