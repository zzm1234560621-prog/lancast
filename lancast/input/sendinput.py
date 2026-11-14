"""Windows ``SendInput`` backend (ctypes).

The pure builders at the bottom are deliberately separate from the actual
``SendInput`` call so they can be unit-tested without moving the user's mouse
or typing on their keyboard.

References (winuser.h): MOUSEINPUT / KEYBDINPUT / HARDWAREINPUT / INPUT,
``KEYEVENTF_*``, ``MOUSEEVENTF_*`` and the ``SM_*VIRTUALSCREEN`` metrics.
"""

from __future__ import annotations

import ctypes
import logging
import os
import threading
import time
from typing import List, Optional, Tuple

from .backend import InputBackend, coerce_int, require_button, require_key_code
from .keycodes import (
    VK_ONLY_KEY_CODES,
    key_info,
)

__all__ = [
    "INPUT",
    "MOUSEINPUT",
    "KEYBDINPUT",
    "HARDWAREINPUT",
    "SendInputBackend",
    "build_key_input",
    "build_button_input",
    "build_text_inputs",
    "build_wheel_inputs",
    "build_move_relative_input",
    "build_absolute_input",
    "normalize_absolute",
    "virtual_screen_rect",
]

logger = logging.getLogger("lancast.input")

# Portable Win32 fixed-width aliases: ctypes.wintypes.DWORD is c_ulong, which
# is 8 bytes on non-Windows and would make sizeof(INPUT) wrong there.
WORD = ctypes.c_uint16
DWORD = ctypes.c_uint32
LONG = ctypes.c_int32
BOOL = ctypes.c_int
UINT = ctypes.c_uint
ULONG_PTR = ctypes.c_uint64 if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_uint32

# INPUT.type
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
INPUT_HARDWARE = 2

# KEYBDINPUT.dwFlags
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008

# MOUSEINPUT.dwFlags
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_XDOWN = 0x0080
MOUSEEVENTF_XUP = 0x0100
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

XBUTTON1 = 0x0001
XBUTTON2 = 0x0002

# GetSystemMetrics indices for the virtual desktop, in physical pixels.
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

# Virtual key codes used by Unicode text translation.
VK_TAB = 0x09
VK_RETURN = 0x0D

WHEEL_DELTA = 120
_WARN_INTERVAL_S = 10.0


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", LONG),
        ("dy", LONG),
        ("mouseData", DWORD),
        ("dwFlags", DWORD),
        ("time", DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", WORD),
        ("wScan", WORD),
        ("dwFlags", DWORD),
        ("time", DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", DWORD),
        ("wParamL", WORD),
        ("wParamH", WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    _anonymous_ = ("_u",)
    _fields_ = [
        ("type", DWORD),
        ("_u", _INPUTUNION),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", LONG), ("y", LONG)]


# ---------------------------------------------------------------------------
# Win32 bindings.  Importing this module on non-Windows must not fail because
# the builders are portable and used by tests.
# ---------------------------------------------------------------------------

_user32 = None
_SEND_INPUT = None
_GET_SYSTEM_METRICS = None
_GET_CURSOR_POS = None

if os.name == "nt":  # pragma: no branch - platform dependent
    try:
        _user32 = ctypes.WinDLL("user32", use_last_error=True)

        _SEND_INPUT = _user32.SendInput
        _SEND_INPUT.argtypes = [UINT, ctypes.POINTER(INPUT), ctypes.c_int]
        _SEND_INPUT.restype = UINT

        _GET_SYSTEM_METRICS = _user32.GetSystemMetrics
        _GET_SYSTEM_METRICS.argtypes = [ctypes.c_int]
        _GET_SYSTEM_METRICS.restype = ctypes.c_int

        _GET_CURSOR_POS = _user32.GetCursorPos
        _GET_CURSOR_POS.argtypes = [ctypes.POINTER(POINT)]
        _GET_CURSOR_POS.restype = BOOL
    except Exception:  # pragma: no cover - unusual Windows environments
        _user32 = None
        _SEND_INPUT = None
        _GET_SYSTEM_METRICS = None
        _GET_CURSOR_POS = None


_warn_lock = threading.Lock()
_last_warning_at: Optional[float] = None


def _warn_partial(sent: int, expected: int) -> None:
    """Log a rate-limited warning when ``SendInput`` dropped events.

    UIPI silently blocks injected input aimed at elevated windows; the usual
    symptom is a short return value.  At most one such warning per 10 s.
    """

    global _last_warning_at
    now = time.monotonic()
    with _warn_lock:
        if _last_warning_at is not None and now - _last_warning_at < _WARN_INTERVAL_S:
            return
        _last_warning_at = now
    logger.warning(
        "SendInput inserted only %d of %d event(s); the target window may be "
        "running elevated (UIPI blocks injected input).",
        sent,
        expected,
    )


def _send_inputs(inputs: List[INPUT]) -> int:
    """Submit one batch; return how many events the OS accepted."""

    if not inputs:
        return 0
    if _SEND_INPUT is None:  # pragma: no cover - non-Windows guard
        raise OSError("SendInput is only available on Windows")
    array = (INPUT * len(inputs))(*inputs)
    sent = int(_SEND_INPUT(len(inputs), array, ctypes.sizeof(INPUT)))
    if sent < len(inputs):
        _warn_partial(sent, len(inputs))
    return sent


def _require_windows() -> None:
    if os.name != "nt" or _SEND_INPUT is None:  # pragma: no cover - non-Windows
        raise OSError("SendInput is only available on Windows")


def _ensure_dpi_awareness() -> None:
    # Imported lazily so importing the pure builders does not pull in the
    # capture stack (and its native dependencies).
    from lancast.capture.dpi import ensure_dpi_awareness

    ensure_dpi_awareness()


# ---------------------------------------------------------------------------
# Pure builders (no SendInput call).
# ---------------------------------------------------------------------------


def _mouse_input(dx: int, dy: int, mouse_data: int, flags: int) -> INPUT:
    event = INPUT()
    event.type = INPUT_MOUSE
    event.mi.dx = int(dx)
    event.mi.dy = int(dy)
    event.mi.mouseData = int(mouse_data) & 0xFFFFFFFF
    event.mi.dwFlags = int(flags)
    event.mi.time = 0
    event.mi.dwExtraInfo = 0
    return event


def _keyboard_input(vk: int, scan: int, flags: int) -> INPUT:
    event = INPUT()
    event.type = INPUT_KEYBOARD
    event.ki.wVk = int(vk) & 0xFFFF
    event.ki.wScan = int(scan) & 0xFFFF
    event.ki.dwFlags = int(flags)
    event.ki.time = 0
    event.ki.dwExtraInfo = 0
    return event


def build_key_input(code: str, down: bool) -> INPUT:
    """Build the KEYBDINPUT event for ``code`` (``ValueError`` if unknown)."""

    code = require_key_code(code)
    vk, scan, extended = key_info(code)
    flags = 0 if down else KEYEVENTF_KEYUP

    if code in VK_ONLY_KEY_CODES:
        # Some applications ignore scan-code-only media keys; virtual keys
        # are the documented way to inject them.
        return _keyboard_input(vk, 0, flags)

    if scan:
        prefix = (scan >> 8) & 0xFF
        if prefix == 0xE1:
            # E1-prefixed keys (Pause) have no single-scan-code form: inject by virtual key.
            return _keyboard_input(vk, 0, flags)
        # MAPVK_VK_TO_VSC_EX reports the E0 prefix in the high byte; SendInput wants
        # the bare scan code in wScan and the prefix expressed as KEYEVENTF_EXTENDEDKEY.
        flags |= KEYEVENTF_SCANCODE
        if extended or prefix == 0xE0:
            flags |= KEYEVENTF_EXTENDEDKEY
        scan &= 0xFF
    return _keyboard_input(vk, scan, flags)


def build_button_input(button: str, down: bool) -> INPUT:
    """Build a mouse button event; ``ValueError`` for an unknown button."""

    button = require_button(button)
    mouse_data = 0
    if button == "left":
        flags = MOUSEEVENTF_LEFTDOWN if down else MOUSEEVENTF_LEFTUP
    elif button == "right":
        flags = MOUSEEVENTF_RIGHTDOWN if down else MOUSEEVENTF_RIGHTUP
    elif button == "middle":
        flags = MOUSEEVENTF_MIDDLEDOWN if down else MOUSEEVENTF_MIDDLEUP
    elif button == "x1":
        flags = MOUSEEVENTF_XDOWN if down else MOUSEEVENTF_XUP
        mouse_data = XBUTTON1
    else:  # "x2"
        flags = MOUSEEVENTF_XDOWN if down else MOUSEEVENTF_XUP
        mouse_data = XBUTTON2
    return _mouse_input(0, 0, mouse_data, flags)


def build_wheel_inputs(dy: int, dx: int = 0) -> List[INPUT]:
    """Build wheel events; ``mouseData`` is a signed 32-bit value in a DWORD."""

    events: List[INPUT] = []
    if dy:
        events.append(_mouse_input(0, 0, int(dy), MOUSEEVENTF_WHEEL))
    if dx:
        events.append(_mouse_input(0, 0, int(dx), MOUSEEVENTF_HWHEEL))
    return events


def build_move_relative_input(dx: int, dy: int) -> INPUT:
    return _mouse_input(int(dx), int(dy), 0, MOUSEEVENTF_MOVE)


def normalize_absolute(
    x: int,
    y: int,
    left: int,
    top: int,
    width: int,
    height: int,
) -> Tuple[int, int]:
    """Map physical virtual-desktop pixels to ``0..65535`` absolute units."""

    def axis(value: int, origin: int, extent: int) -> int:
        if extent <= 1:
            return 0
        scaled = round((value - origin) * 65535 / (extent - 1))
        return max(0, min(65535, int(scaled)))

    return axis(int(x), int(left), int(width)), axis(int(y), int(top), int(height))


def build_absolute_input(
    x: int,
    y: int,
    left: int,
    top: int,
    width: int,
    height: int,
) -> INPUT:
    nx, ny = normalize_absolute(x, y, left, top, width, height)
    return _mouse_input(
        nx,
        ny,
        0,
        MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
    )


def _utf16_units(text: str) -> List[int]:
    """UTF-16 code units, so astral characters become surrogate pairs."""

    data = text.encode("utf-16-le", "surrogatepass")
    return [
        int.from_bytes(data[index : index + 2], "little")
        for index in range(0, len(data), 2)
    ]


def build_text_inputs(s: str) -> List[INPUT]:
    """Build all keyboard events for :meth:`SendInputBackend.text`.

    ``\\n`` and ``\\t`` become virtual Enter/Tab events; everything else is a
    ``KEYEVENTF_UNICODE`` down/up pair per UTF-16 code unit.
    """

    if not isinstance(s, str):
        raise ValueError(f"text must be a string, got {type(s).__name__}")

    events: List[INPUT] = []
    for char in s:
        if char == "\n" or char == "\t":
            vk = VK_RETURN if char == "\n" else VK_TAB
            events.append(_keyboard_input(vk, 0, 0))
            events.append(_keyboard_input(vk, 0, KEYEVENTF_KEYUP))
            continue
        for unit in _utf16_units(char):
            events.append(_keyboard_input(0, unit, KEYEVENTF_UNICODE))
            events.append(
                _keyboard_input(0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)
            )
    return events


def virtual_screen_rect() -> Tuple[int, int, int, int]:
    """``(left, top, width, height)`` of the virtual desktop in physical pixels."""

    if _GET_SYSTEM_METRICS is None:  # pragma: no cover - non-Windows guard
        raise OSError("GetSystemMetrics is only available on Windows")
    return (
        int(_GET_SYSTEM_METRICS(SM_XVIRTUALSCREEN)),
        int(_GET_SYSTEM_METRICS(SM_YVIRTUALSCREEN)),
        int(_GET_SYSTEM_METRICS(SM_CXVIRTUALSCREEN)),
        int(_GET_SYSTEM_METRICS(SM_CYVIRTUALSCREEN)),
    )


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


class SendInputBackend(InputBackend):
    """Injects input through ``user32.SendInput``."""

    name = "sendinput"

    def __init__(self) -> None:
        _require_windows()
        # Coordinates are reported and injected in physical pixels.
        _ensure_dpi_awareness()

    def move_relative(self, dx: int, dy: int) -> None:
        _send_inputs(
            [build_move_relative_input(coerce_int(dx, "dx"), coerce_int(dy, "dy"))]
        )

    def move_absolute(self, x: int, y: int) -> None:
        x = coerce_int(x, "x")
        y = coerce_int(y, "y")
        left, top, width, height = virtual_screen_rect()
        _send_inputs([build_absolute_input(x, y, left, top, width, height)])

    def button(self, button: str, down: bool) -> None:
        _send_inputs([build_button_input(button, down)])

    def wheel(self, dy: int, dx: int = 0) -> None:
        dy = coerce_int(dy, "dy")
        dx = coerce_int(dx, "dx")
        _send_inputs(build_wheel_inputs(dy, dx))

    def key(self, code: str, down: bool) -> None:
        _send_inputs([build_key_input(code, down)])

    def text(self, s: str) -> None:
        _send_inputs(build_text_inputs(s))

    def cursor_position(self) -> Tuple[int, int]:
        if _GET_CURSOR_POS is None:  # pragma: no cover - non-Windows guard
            raise OSError("GetCursorPos is only available on Windows")
        point = POINT()
        if not _GET_CURSOR_POS(ctypes.byref(point)):
            raise OSError("GetCursorPos failed")
        return int(point.x), int(point.y)
