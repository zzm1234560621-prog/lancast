"""Pure SendInput builders: struct layout, flags, normalisation, Unicode."""

from __future__ import annotations

import ctypes

import pytest

from lancast.input import key_info
from lancast.input.sendinput import (
    HARDWAREINPUT,
    INPUT,
    KEYBDINPUT,
    MOUSEINPUT,
    KEYEVENTF_EXTENDEDKEY,
    KEYEVENTF_KEYUP,
    KEYEVENTF_SCANCODE,
    KEYEVENTF_UNICODE,
    MOUSEEVENTF_ABSOLUTE,
    MOUSEEVENTF_HWHEEL,
    MOUSEEVENTF_LEFTDOWN,
    MOUSEEVENTF_LEFTUP,
    MOUSEEVENTF_MOVE,
    MOUSEEVENTF_VIRTUALDESK,
    MOUSEEVENTF_WHEEL,
    MOUSEEVENTF_XDOWN,
    MOUSEEVENTF_XUP,
    XBUTTON1,
    XBUTTON2,
    build_absolute_input,
    build_button_input,
    build_key_input,
    build_text_inputs,
    build_wheel_inputs,
    normalize_absolute,
)


def test_input_is_40_bytes_on_64_bit():
    if ctypes.sizeof(ctypes.c_void_p) != 8:
        pytest.skip("64-bit layout check")
    assert ctypes.sizeof(INPUT) == 40
    assert ctypes.sizeof(MOUSEINPUT) == 32
    assert ctypes.sizeof(KEYBDINPUT) == 24
    assert ctypes.sizeof(HARDWAREINPUT) == 8
    # 4-byte type + 4 bytes alignment padding before the 8-byte-aligned union.
    assert INPUT._u.offset == 8


def test_key_uses_scancode_and_extended_flag():
    down = build_key_input("KeyA", True)
    up = build_key_input("KeyA", False)
    vk, scan, extended = key_info("KeyA")
    assert down.type == 1
    assert down.ki.wVk == vk
    assert down.ki.wScan == scan
    assert down.ki.dwFlags & KEYEVENTF_SCANCODE
    assert not down.ki.dwFlags & KEYEVENTF_KEYUP
    assert extended is False
    assert up.ki.dwFlags & KEYEVENTF_KEYUP


def test_extended_key_sets_extended_and_scancode_flags():
    event = build_key_input("ArrowLeft", True)
    vk, scan, extended = key_info("ArrowLeft")
    assert extended is True
    assert event.ki.wVk == vk
    assert event.ki.wScan == scan
    assert event.ki.dwFlags & KEYEVENTF_SCANCODE
    assert event.ki.dwFlags & KEYEVENTF_EXTENDEDKEY


def test_media_keys_use_virtual_key_only():
    for code in ("AudioVolumeUp", "AudioVolumeMute", "MediaPlayPause", "BrowserBack"):
        event = build_key_input(code, True)
        assert event.ki.wVk == key_info(code)[0]
        assert event.ki.wScan == 0
        assert not event.ki.dwFlags & KEYEVENTF_SCANCODE
        assert not event.ki.dwFlags & KEYEVENTF_EXTENDEDKEY


def test_unknown_key_raises_value_error():
    with pytest.raises(ValueError):
        build_key_input("KeyAA", True)


def test_absolute_normalisation_corners():
    assert normalize_absolute(0, 0, 0, 0, 1920, 1080) == (0, 0)
    assert normalize_absolute(1919, 1079, 0, 0, 1920, 1080) == (65535, 65535)


def test_absolute_normalisation_negative_virtual_desktop():
    left, top, width, height = -1920, -200, 3840, 1280
    assert normalize_absolute(-1920, -200, left, top, width, height) == (0, 0)
    assert normalize_absolute(1919, 1079, left, top, width, height) == (
        65535,
        65535,
    )


def test_absolute_normalisation_clamps_out_of_bounds():
    assert normalize_absolute(-99999, -99999, 0, 0, 100, 100) == (0, 0)
    assert normalize_absolute(99999, 99999, 0, 0, 100, 100) == (65535, 65535)
    # Degenerate geometry must not divide by zero.
    assert normalize_absolute(5, 5, 0, 0, 1, 1) == (0, 0)


def test_absolute_input_flags():
    event = build_absolute_input(1919, 1079, 0, 0, 1920, 1080)
    assert event.type == 0
    assert event.mi.dx == 65535
    assert event.mi.dy == 65535
    assert event.mi.dwFlags & MOUSEEVENTF_MOVE
    assert event.mi.dwFlags & MOUSEEVENTF_ABSOLUTE
    assert event.mi.dwFlags & MOUSEEVENTF_VIRTUALDESK


def test_negative_wheel_is_encoded_as_32_bit_twos_complement():
    events = build_wheel_inputs(-120)
    assert len(events) == 1
    event = events[0]
    assert event.type == 0
    assert event.mi.dwFlags == MOUSEEVENTF_WHEEL
    assert event.mi.mouseData == 0xFFFFFF88
    assert event.mi.mouseData != 120


def test_horizontal_wheel_and_both_axes():
    events = build_wheel_inputs(0, -120)
    assert len(events) == 1
    assert events[0].mi.dwFlags == MOUSEEVENTF_HWHEEL
    assert events[0].mi.mouseData == 0xFFFFFF88

    both = build_wheel_inputs(240, 120)
    assert len(both) == 2
    assert both[0].mi.dwFlags == MOUSEEVENTF_WHEEL
    assert both[0].mi.mouseData == 240
    assert both[1].mi.dwFlags == MOUSEEVENTF_HWHEEL
    assert both[1].mi.mouseData == 120


def test_mouse_buttons_flags_and_xbuttons():
    assert build_button_input("left", True).mi.dwFlags == MOUSEEVENTF_LEFTDOWN
    assert build_button_input("left", False).mi.dwFlags == MOUSEEVENTF_LEFTUP

    x1_down = build_button_input("x1", True)
    x1_up = build_button_input("x1", False)
    assert x1_down.mi.dwFlags == MOUSEEVENTF_XDOWN
    assert x1_down.mi.mouseData == XBUTTON1
    assert x1_up.mi.dwFlags == MOUSEEVENTF_XUP

    x2 = build_button_input("x2", True)
    assert x2.mi.dwFlags == MOUSEEVENTF_XDOWN
    assert x2.mi.mouseData == XBUTTON2


def test_unknown_button_raises_value_error():
    with pytest.raises(ValueError):
        build_button_input("wheel", True)


def test_text_units_for_bmp_and_astral_characters():
    events = build_text_inputs("a你😀")
    # "a" + "你" + U+1F600 as two UTF-16 surrogate code units.
    down = events[0::2]
    up = events[1::2]
    assert len(events) == 8
    assert [event.ki.wScan for event in down] == [0x61, 0x4F60, 0xD83D, 0xDE00]
    assert [event.ki.wScan for event in up] == [0x61, 0x4F60, 0xD83D, 0xDE00]
    for event in events:
        assert event.type == 1
        assert event.ki.wVk == 0
        assert event.ki.dwFlags & KEYEVENTF_UNICODE
    for event in down:
        assert not event.ki.dwFlags & KEYEVENTF_KEYUP
    for event in up:
        assert event.ki.dwFlags & KEYEVENTF_KEYUP


def test_text_newline_and_tab_use_virtual_keys():
    events = build_text_inputs("\n\t")
    assert len(events) == 4
    assert [event.ki.wVk for event in events] == [0x0D, 0x0D, 0x09, 0x09]
    assert [event.ki.wScan for event in events] == [0, 0, 0, 0]
    assert not events[0].ki.dwFlags & KEYEVENTF_UNICODE
    assert events[1].ki.dwFlags & KEYEVENTF_KEYUP


def test_text_is_empty_for_empty_string():
    assert build_text_inputs("") == []


def test_text_rejects_non_string():
    with pytest.raises(ValueError):
        build_text_inputs(None)  # type: ignore[arg-type]
