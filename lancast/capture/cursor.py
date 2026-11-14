"""Mouse pointer state via ``GetCursorInfo``.

``GetCursorInfo`` reports screen coordinates in physical pixels once the
process is per-monitor DPI aware.  Standard cursor handles are loaded once
at import time and compared by handle value, which avoids drawing or
inspecting cursor bitmaps.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import logging
import os
import threading

from .base import CursorState, DisplayInfo

logger = logging.getLogger("lancast.capture")

CURSOR_SHOWING = 0x00000001

# Standard cursor resource ids (winuser.h IDC_*).
_IDC_ARROW = 32512
_IDC_IBEAM = 32513
_IDC_WAIT = 32514
_IDC_CROSS = 32515
_IDC_SIZENWSE = 32642
_IDC_SIZENESW = 32643
_IDC_SIZEWE = 32644
_IDC_SIZENS = 32645
_IDC_SIZEALL = 32646
_IDC_NO = 32648
_IDC_HAND = 32649
_IDC_APPSTARTING = 32650

_SHAPE_BY_RESOURCE = {
    _IDC_ARROW: "arrow",
    _IDC_IBEAM: "ibeam",
    _IDC_HAND: "hand",
    _IDC_WAIT: "wait",
    _IDC_APPSTARTING: "wait",
    _IDC_CROSS: "cross",
    _IDC_SIZEALL: "move",
    _IDC_SIZENS: "size_ns",
    _IDC_SIZEWE: "size_we",
    _IDC_SIZENWSE: "size_nwse",
    _IDC_SIZENESW: "size_nesw",
    _IDC_NO: "no",
}

_IS_WINDOWS = os.name == "nt"
_lock = threading.Lock()

_SHAPE_BY_HANDLE: dict[int, str] = {}
_GET_CURSOR_INFO = None


class _CURSORINFO_STRUCT(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hCursor", wintypes.HANDLE),
        ("ptScreenPos", wintypes.POINT),
    ]


def _build_cursor_tables() -> None:
    """Load standard cursors and prepare the ctypes signatures (Windows only)."""

    global _GET_CURSOR_INFO
    if not _IS_WINDOWS:
        return
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        load_cursor = user32.LoadCursorW
        load_cursor.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
        load_cursor.restype = ctypes.c_void_p

        for resource_id, shape in _SHAPE_BY_RESOURCE.items():
            handle = load_cursor(None, ctypes.c_void_p(resource_id))
            if handle:
                _SHAPE_BY_HANDLE[int(handle)] = shape

        get_cursor_info = user32.GetCursorInfo
        get_cursor_info.argtypes = [ctypes.POINTER(_CURSORINFO_STRUCT)]
        get_cursor_info.restype = wintypes.BOOL
        _GET_CURSOR_INFO = get_cursor_info
    except Exception:  # pragma: no cover - unusual Windows environments
        logger.debug("Could not initialise cursor tables.", exc_info=True)
        _GET_CURSOR_INFO = None


_build_cursor_tables()


def _default_state() -> CursorState:
    return CursorState(x=0.0, y=0.0, visible=False, shape="arrow")


def get_cursor(display: DisplayInfo) -> CursorState:
    """Return the pointer position normalised to ``display``.

    The result is thread safe.  When the pointer is hidden or outside the
    display, ``visible`` is ``False`` and the coordinates are clamped to
    ``0..1`` so clients always receive a valid unit position.
    """

    if not _IS_WINDOWS or _GET_CURSOR_INFO is None:
        return _default_state()
    if display.width <= 0 or display.height <= 0:
        return _default_state()

    info = _CURSORINFO_STRUCT()
    info.cbSize = ctypes.sizeof(_CURSORINFO_STRUCT)

    with _lock:
        ok = bool(_GET_CURSOR_INFO(ctypes.byref(info)))
        handle = int(info.hCursor or 0)
        shape = _SHAPE_BY_HANDLE.get(handle, "other")

    if not ok:
        return CursorState(x=0.0, y=0.0, visible=False, shape="other")

    px = int(info.ptScreenPos.x)
    py = int(info.ptScreenPos.y)
    nx = (px - display.x) / float(display.width)
    ny = (py - display.y) / float(display.height)
    on_display = 0.0 <= nx < 1.0 and 0.0 <= ny < 1.0
    showing = bool(int(info.flags) & CURSOR_SHOWING)
    return CursorState(
        x=min(max(nx, 0.0), 1.0),
        y=min(max(ny, 0.0), 1.0),
        visible=bool(showing and on_display),
        shape=shape,
    )
