"""Key code contract: exactly PROTOCOL.md §3.7, VKs and extended flags."""

from __future__ import annotations

import os

import pytest

from lancast.input import BUTTONS, KEY_CODES, key_info
from lancast.input.keycodes import EXTENDED_KEY_CODES

# Explicitly expanded from PROTOCOL.md §3.7 (no reading the docs at test time).
EXPECTED_KEY_CODES = frozenset(
    {f"Key{letter}" for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"}
    | {f"Digit{digit}" for digit in "0123456789"}
    | {f"F{number}" for number in range(1, 25)}
    | {
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
    }
    | {f"Numpad{digit}" for digit in "0123456789"}
    | {
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
    }
)


def test_key_codes_exactly_match_protocol():
    assert KEY_CODES == EXPECTED_KEY_CODES
    assert len(KEY_CODES) == 127


def test_buttons_are_the_documented_five():
    assert BUTTONS == ("left", "right", "middle", "x1", "x2")


def test_every_code_has_a_nonzero_vk_and_scan_on_windows():
    for code in EXPECTED_KEY_CODES:
        vk, scan, extended = key_info(code)
        assert isinstance(vk, int) and vk > 0, code
        assert isinstance(scan, int) and scan >= 0, code
        assert isinstance(extended, bool), code
        if os.name == "nt":
            assert scan > 0, code


def test_unknown_code_raises_value_error():
    with pytest.raises(ValueError):
        key_info("KeyAA")
    with pytest.raises(ValueError):
        key_info("")


def test_spot_check_virtual_keys():
    expected = {
        "KeyA": 0x41,
        "KeyZ": 0x5A,
        "Digit0": 0x30,
        "Digit9": 0x39,
        "F1": 0x70,
        "F24": 0x87,
        "Space": 0x20,
        "Pause": 0x13,
        "MetaLeft": 0x5B,
        "MetaRight": 0x5C,
        "ContextMenu": 0x5D,
        "Backquote": 0xC0,
        "IntlBackslash": 0xE2,
        "Numpad0": 0x60,
        "NumpadEnter": 0x0D,
        "NumpadDivide": 0x6F,
        "MediaPlayPause": 0xB3,
        "AudioVolumeUp": 0xAF,
        "AudioVolumeMute": 0xAD,
        "BrowserBack": 0xA6,
        "BrowserRefresh": 0xA8,
    }
    for code, vk in expected.items():
        assert key_info(code)[0] == vk, code


def test_extended_key_flags():
    for code in EXTENDED_KEY_CODES:
        assert key_info(code)[2] is True, code
    assert EXTENDED_KEY_CODES == frozenset(
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


@pytest.mark.parametrize(
    "code",
    ["KeyA", "Enter", "Numpad0", "ShiftLeft", "ControlLeft", "AltLeft", "Pause"],
)
def test_non_extended_keys_are_not_extended(code):
    assert key_info(code)[2] is False
