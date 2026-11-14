"""Enumerate Windows displays in physical pixels.

``EnumDisplayMonitors`` + ``GetMonitorInfoW(MONITORINFOEXW)`` is the
authoritative source for the virtual-desktop geometry, including the OS
device name (``\\\\.\\DISPLAY1``) that DXGI and MSS also use.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import logging
import os
from dataclasses import dataclass

from .base import DisplayInfo
from .dpi import ensure_dpi_awareness

logger = logging.getLogger("lancast.capture")

MONITORINFOF_PRIMARY = 0x00000001
_CCHDEVICENAME = 32


class _MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * _CCHDEVICENAME),
    ]


@dataclass
class _RawMonitor:
    device: str
    x: int
    y: int
    width: int
    height: int
    primary: bool


def _enum_monitors_windows() -> list[_RawMonitor]:
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
    except OSError:  # pragma: no cover - only on non-Windows
        logger.debug("user32 is unavailable; no displays enumerated.")
        return []

    monitor_enum_proc = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HMONITOR,
        wintypes.HDC,
        ctypes.POINTER(wintypes.RECT),
        wintypes.LPARAM,
    )
    user32.EnumDisplayMonitors.argtypes = [
        wintypes.HDC,
        ctypes.POINTER(wintypes.RECT),
        monitor_enum_proc,
        wintypes.LPARAM,
    ]
    user32.EnumDisplayMonitors.restype = wintypes.BOOL
    user32.GetMonitorInfoW.argtypes = [
        wintypes.HMONITOR,
        ctypes.POINTER(_MONITORINFOEXW),
    ]
    user32.GetMonitorInfoW.restype = wintypes.BOOL

    found: list[_RawMonitor] = []

    def _callback(hmonitor, _hdc, _rect, _lparam):
        info = _MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(_MONITORINFOEXW)
        if not user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
            return True
        rc = info.rcMonitor
        width = int(rc.right) - int(rc.left)
        height = int(rc.bottom) - int(rc.top)
        if width <= 0 or height <= 0:
            return True
        found.append(
            _RawMonitor(
                device=str(info.szDevice),
                x=int(rc.left),
                y=int(rc.top),
                width=width,
                height=height,
                primary=bool(int(info.dwFlags) & MONITORINFOF_PRIMARY),
            )
        )
        return True

    callback = monitor_enum_proc(_callback)
    if not user32.EnumDisplayMonitors(None, None, callback, 0):
        err = ctypes.get_last_error()
        logger.warning("EnumDisplayMonitors failed (WinError %s).", err)
    return found


def list_displays() -> list[DisplayInfo]:
    """Return connected displays, primary first, then sorted by ``(x, y)``.

    Coordinates and sizes are physical pixels.  Only monitors that Win32
    actually reports are returned; virtual display drivers that are not
    attached to the desktop do not appear.
    """

    ensure_dpi_awareness()
    if os.name != "nt":
        return []

    raw = _enum_monitors_windows()
    raw.sort(key=lambda m: (0 if m.primary else 1, m.x, m.y, m.device))

    displays: list[DisplayInfo] = []
    for index, monitor in enumerate(raw):
        displays.append(
            DisplayInfo(
                index=index,
                id=monitor.device,
                name=f"Display {index + 1}",
                x=monitor.x,
                y=monitor.y,
                width=monitor.width,
                height=monitor.height,
                primary=monitor.primary,
            )
        )
    return displays
