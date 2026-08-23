"""Local OS-level USB acquisition, in-process only.

`AcquisitionSource` is the seam Event Source depends on. Two implementations
share it so main.py never changes between environments:

- `MockUsbMonitor` — macOS dev/CI. No pyudev, no OS access. Tests call
  `simulate_insert(...)` directly, which is exactly the same in-process path
  a real device event would take (the callback signature is identical), just
  without a real udev event driving it.
- `PyudevUsbMonitor` — the Ubuntu ARM64 VM. Real `pyudev.Monitor` +
  `Observer` watching `subsystem="block"` events, per CLAUDE-2.md §12.

Neither implementation is reachable over HTTP. There is no network call
anywhere in this module — `on_event` is a plain Python callback invoked
in-process, per A.5 ("USB Monitor — in-process, не через HTTP/curl").
"""

from __future__ import annotations

import sys
from typing import Callable, Optional, Protocol, TypedDict


class DeviceAttrs(TypedDict, total=False):
    serial: Optional[str]
    label: Optional[str]
    raw: dict


OnDeviceEvent = Callable[[DeviceAttrs], None]


class AcquisitionSource(Protocol):
    def start(self, on_event: OnDeviceEvent) -> None: ...
    def stop(self) -> None: ...


class MockUsbMonitor:
    """Test/dev double. No OS access — attrs are supplied directly by the caller."""

    def __init__(self) -> None:
        self._on_event: Optional[OnDeviceEvent] = None
        self._active = False

    def start(self, on_event: OnDeviceEvent) -> None:
        self._on_event = on_event
        self._active = True

    def stop(self) -> None:
        # Mirrors a real Observer.stop(): once stopped, no further events
        # reach Event Source at all — this is what T16 exercises.
        self._active = False

    def simulate_insert(
        self, serial: Optional[str] = None, label: Optional[str] = None, raw: Optional[dict] = None
    ) -> None:
        if not self._active or self._on_event is None:
            return
        self._on_event({"serial": serial, "label": label, "raw": raw or {}})


class PyudevUsbMonitor:
    """Real Linux acquisition path (Ubuntu ARM64 VM). See runbook for setup."""

    def __init__(self) -> None:
        self._observer = None
        self._on_event: Optional[OnDeviceEvent] = None

    def start(self, on_event: OnDeviceEvent) -> None:
        if sys.platform != "linux":
            raise RuntimeError(
                "PyudevUsbMonitor requires Linux/udev; use MockUsbMonitor on macOS"
            )
        import pyudev  # local import: keeps this file importable on macOS

        self._on_event = on_event
        context = pyudev.Context()
        monitor = pyudev.Monitor.from_netlink(context)
        monitor.filter_by(subsystem="block")
        self._observer = pyudev.MonitorObserver(monitor, callback=self._handle_udev_device)
        self._observer.start()

    def stop(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer = None

    def _handle_udev_device(self, device) -> None:
        # Only whole-disk "add" events — ignore partition add/change noise
        # so one physical insert doesn't fire the callback multiple times.
        if device.action != "add" or device.get("DEVTYPE") != "disk":
            return
        attrs: DeviceAttrs = {
            "serial": device.get("ID_SERIAL_SHORT"),
            "label": device.get("ID_FS_LABEL"),
            "raw": dict(device.properties),
        }
        assert self._on_event is not None
        self._on_event(attrs)


def default_monitor() -> AcquisitionSource:
    if sys.platform == "linux":
        return PyudevUsbMonitor()
    return MockUsbMonitor()
