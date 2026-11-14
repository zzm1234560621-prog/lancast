"""Display enumeration tests (Windows only for the real checks)."""

from __future__ import annotations

import ctypes
import os

import pytest

from lancast.capture import DisplayInfo, ensure_dpi_awareness, list_displays

pytestmark = pytest.mark.skipif(
    os.name != "nt", reason="Win32 display enumeration requires Windows"
)


def test_list_displays_contract():
    ensure_dpi_awareness()
    displays = list_displays()

    assert displays, "at least one display must be reported"
    assert all(isinstance(display, DisplayInfo) for display in displays)

    # Primary first, then by (x, y); indices and labels follow that order.
    assert displays[0].primary
    assert [d.index for d in displays] == list(range(len(displays)))
    assert [d.name for d in displays] == [
        f"Display {i + 1}" for i in range(len(displays))
    ]

    rest = displays[1:]
    assert rest == sorted(rest, key=lambda d: (d.x, d.y, d.id))

    ids = [d.id for d in displays]
    assert len(ids) == len(set(ids))
    for display in displays:
        assert display.id.startswith("\\\\.\\")
        assert display.width > 0
        assert display.height > 0


def test_primary_geometry_is_in_physical_pixels():
    # GetSystemMetrics(SM_CXSCREEN/SM_CYSCREEN) also reports physical pixels
    # once the process is per-monitor DPI aware.  At 200% scaling an unaware
    # process would see 1440x900 instead of the real 2880x1800.
    displays = list_displays()
    user32 = ctypes.windll.user32
    assert displays[0].width == int(user32.GetSystemMetrics(0))
    assert displays[0].height == int(user32.GetSystemMetrics(1))


def test_ensure_dpi_awareness_is_idempotent():
    ensure_dpi_awareness()
    ensure_dpi_awareness()
