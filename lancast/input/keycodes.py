"""Keyboard code tables for remote-control injection.

``KeyboardEvent.code`` names (PROTOCOL.md §3.7) are the wire format; here they
are translated to Win32 virtual-key codes and hardware scan codes.

The scan code is looked up with ``MapVirtualKeyW(vk, MAPVK_VK_TO_VSC_EX)`` on
Windows.  That function reports extended scan codes with a ``0xE0`` (or
``0xE1``) high byte for some keys and a plain byte for others, so the
authoritative "is this an extended key" information lives in
:data:`EXTENDED_KEY_CODES` instead of being inferred from the scan code.
Non-Windows platforms return scan code ``0``.
"""

from __future__ import annotations

import ctypes
import os
from functools import lru_cache

__all__ = [
    "KEY_CODES",
    "BUTTONS",
    "EXTENDED_KEY_CODES",
    "VK_ONLY_KEY_CODES",
    "key_info",
]


def _letters() -> tuple[str, ...]:
    return tuple(f"Key{chr(ord('A') + i)}" for i in range(26))


def _digits() -> tuple[str, ...]:
    return tuple(f"Digit{i}" for i in range(10))


def _function_keys() -> tuple[str, ...]:
    return tuple(f"F{i}" for i in range(1, 25))


def _numpads() -> tuple[str, ...]:
    return tuple(f"Numpad{i}" for i in range(10))


#: Exactly the KeyboardEvent.code list supported by PROTOCOL.md §3.7.
KEY_CODES: frozenset[str] = frozenset(
    _letters()
    + _digits()
    + _function_keys()
    + (
        "ArrowUp",
        "ArrowDown",
        "ArrowLeft",
        "ArrowRight",
        "Home",
        "End",
        "PageUp",
        "PageDown",
        "Insert",
        "Delete",
        "Backspace",
        "Tab",
        "Enter",
        "Space",
        "Escape",
        "CapsLock",
        "NumLock",
        "ScrollLock",
        "PrintScreen",
        "Pause",
        "ContextMenu",
        "ShiftLeft",
        "ShiftRight",
        "ControlLeft",
        "ControlRight",
        "AltLeft",
        "AltRight",
        "MetaLeft",
        "MetaRight",
        "Minus",
        "Equal",
        "BracketLeft",
        "BracketRight",
        "Backslash",
        "Semicolon",
        "Quote",
        "Backquote",
        "Comma",
        "Period",
        "Slash",
        "IntlBackslash",
    )
    + _numpads()
    + (
        "NumpadAdd",
        "NumpadSubtract",
        "NumpadMultiply",
        "NumpadDivide",
        "NumpadDecimal",
        "NumpadEnter",
        "MediaPlayPause",
        "MediaTrackNext",
        "MediaTrackPrevious",
        "MediaStop",
        "AudioVolumeUp",
        "AudioVolumeDown",
        "AudioVolumeMute",
        "BrowserBack",
        "BrowserForward",
        "BrowserRefresh",
    )
)

#: Mouse button names accepted by ``InputBackend.button``.
BUTTONS: tuple[str, ...] = ("left", "right", "middle", "x1", "x2")

#: Keys whose scan code is prefixed with ``0xE0`` (``KEYEVENTF_EXTENDEDKEY``).
EXTENDED_KEY_CODES: frozenset[str] = frozenset(
    {
        "ArrowUp",
        "ArrowDown",
        "ArrowLeft",
        "ArrowRight",
        "Insert",
        "Delete",
        "Home",
        "End",
        "PageUp",
        "PageDown",
        "ControlRight",
        "AltRight",
        "MetaLeft",
        "MetaRight",
        "ContextMenu",
        "NumpadDivide",
        "NumpadEnter",
        "PrintScreen",
        "NumLock",
    }
)

#: Media/volume/browser keys are injected by virtual key only: their scan
#: codes are synthetic and some foreground applications ignore scan-code-only
#: key events for them.
VK_ONLY_KEY_CODES: frozenset[str] = frozenset(
    {
        "MediaPlayPause",
        "MediaTrackNext",
        "MediaTrackPrevious",
        "MediaStop",
        "AudioVolumeUp",
        "AudioVolumeDown",
        "AudioVolumeMute",
        "BrowserBack",
        "BrowserForward",
        "BrowserRefresh",
        # MapVirtualKey reports SysRq (0x54) for VK_SNAPSHOT and an E1 sequence for
        # Pause; neither round-trips through scan-code injection, the VK does.
        "PrintScreen",
        "Pause",
    }
)


def _build_vk_table() -> dict[str, int]:
    table: dict[str, int] = {}
    for index, code in enumerate(_letters()):
        table[code] = 0x41 + index  # VK_A .. VK_Z
    for index, code in enumerate(_digits()):
        table[code] = 0x30 + index  # VK_0 .. VK_9
    for index, code in enumerate(_function_keys()):
        table[code] = 0x70 + index  # VK_F1 .. VK_F24 (0x70 .. 0x87)
    for index, code in enumerate(_numpads()):
        table[code] = 0x60 + index  # VK_NUMPAD0 .. VK_NUMPAD9

    table.update(
        {
            "ArrowLeft": 0x25,  # VK_LEFT
            "ArrowUp": 0x26,  # VK_UP
            "ArrowRight": 0x27,  # VK_RIGHT
            "ArrowDown": 0x28,  # VK_DOWN
            "Home": 0x24,  # VK_HOME
            "End": 0x23,  # VK_END
            "PageUp": 0x21,  # VK_PRIOR
            "PageDown": 0x22,  # VK_NEXT
            "Insert": 0x2D,  # VK_INSERT
            "Delete": 0x2E,  # VK_DELETE
            "Backspace": 0x08,  # VK_BACK
            "Tab": 0x09,  # VK_TAB
            "Enter": 0x0D,  # VK_RETURN
            "Space": 0x20,  # VK_SPACE
            "Escape": 0x1B,  # VK_ESCAPE
            "CapsLock": 0x14,  # VK_CAPITAL
            "NumLock": 0x90,  # VK_NUMLOCK
            "ScrollLock": 0x91,  # VK_SCROLL
            "PrintScreen": 0x2C,  # VK_SNAPSHOT
            "Pause": 0x13,  # VK_PAUSE
            "ContextMenu": 0x5D,  # VK_APPS
            "ShiftLeft": 0xA0,  # VK_LSHIFT
            "ShiftRight": 0xA1,  # VK_RSHIFT
            "ControlLeft": 0xA2,  # VK_LCONTROL
            "ControlRight": 0xA3,  # VK_RCONTROL
            "AltLeft": 0xA4,  # VK_LMENU
            "AltRight": 0xA5,  # VK_RMENU
            "MetaLeft": 0x5B,  # VK_LWIN
            "MetaRight": 0x5C,  # VK_RWIN
            "Minus": 0xBD,  # VK_OEM_MINUS
            "Equal": 0xBB,  # VK_OEM_PLUS
            "BracketLeft": 0xDB,  # VK_OEM_4
            "BracketRight": 0xDD,  # VK_OEM_6
            "Backslash": 0xDC,  # VK_OEM_5
            "Semicolon": 0xBA,  # VK_OEM_1
            "Quote": 0xDE,  # VK_OEM_7
            "Backquote": 0xC0,  # VK_OEM_3
            "Comma": 0xBC,  # VK_OEM_COMMA
            "Period": 0xBE,  # VK_OEM_PERIOD
            "Slash": 0xBF,  # VK_OEM_2
            "IntlBackslash": 0xE2,  # VK_OEM_102
            "NumpadAdd": 0x6B,  # VK_ADD
            "NumpadSubtract": 0x6D,  # VK_SUBTRACT
            "NumpadMultiply": 0x6A,  # VK_MULTIPLY
            "NumpadDivide": 0x6F,  # VK_DIVIDE
            "NumpadDecimal": 0x6E,  # VK_DECIMAL
            "NumpadEnter": 0x0D,  # VK_RETURN, distinguished by extended flag
            "MediaPlayPause": 0xB3,  # VK_MEDIA_PLAY_PAUSE
            "MediaTrackNext": 0xB0,  # VK_MEDIA_NEXT_TRACK
            "MediaTrackPrevious": 0xB1,  # VK_MEDIA_PREV_TRACK
            "MediaStop": 0xB2,  # VK_MEDIA_STOP
            "AudioVolumeUp": 0xAF,  # VK_VOLUME_UP
            "AudioVolumeDown": 0xAE,  # VK_VOLUME_DOWN
            "AudioVolumeMute": 0xAD,  # VK_VOLUME_MUTE
            "BrowserBack": 0xA6,  # VK_BROWSER_BACK
            "BrowserForward": 0xA7,  # VK_BROWSER_FORWARD
            "BrowserRefresh": 0xA8,  # VK_BROWSER_REFRESH
        }
    )
    return table


_VK_BY_CODE: dict[str, int] = _build_vk_table()

# MAPVK_VK_TO_VSC_EX: translate a virtual key to an extended-aware scan code.
_MAPVK_VK_TO_VSC_EX = 4

_MAP_VIRTUAL_KEY = None
if os.name == "nt":  # pragma: no branch - platform dependent
    try:
        _user32 = ctypes.WinDLL("user32", use_last_error=True)
        _MAP_VIRTUAL_KEY = _user32.MapVirtualKeyW
        _MAP_VIRTUAL_KEY.argtypes = [ctypes.c_uint, ctypes.c_uint]
        _MAP_VIRTUAL_KEY.restype = ctypes.c_uint
    except Exception:  # pragma: no cover - unusual Windows environments
        _MAP_VIRTUAL_KEY = None


def _scan_code(vk: int) -> int:
    """Return the hardware scan code for ``vk`` (0 when unavailable)."""

    if _MAP_VIRTUAL_KEY is None:
        return 0
    try:
        return int(_MAP_VIRTUAL_KEY(vk, _MAPVK_VK_TO_VSC_EX))
    except Exception:  # pragma: no cover - defensive
        return 0


@lru_cache(maxsize=None)
def key_info(code: str) -> tuple[int, int, bool]:
    """Return ``(virtual_key, scan_code, is_extended)`` for a key code.

    Raises ``ValueError`` for names outside :data:`KEY_CODES`.
    """

    try:
        vk = _VK_BY_CODE[code]
    except (KeyError, TypeError):
        raise ValueError(f"Unknown key code {code!r}") from None
    return vk, _scan_code(vk), code in EXTENDED_KEY_CODES
