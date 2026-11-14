"""Unit tests for clean_text / clean_name sanitisation."""

from __future__ import annotations

import pytest

from lancast.auth import clean_name, clean_text


def test_drops_controls_and_bidi_overrides():
    raw = "  a\x00b\x07c\n d\t e \u202e f \u2066 g \u2069 h \u200e i  "
    assert clean_text(raw, 100) == "abc d e f g h i"


def test_collapses_whitespace_and_keeps_single_line():
    assert clean_text("  hello \t\n  world  ", 100) == "hello world"
    assert clean_text("a   b", 100) == "a b"
    assert "\n" not in clean_text("a\nb", 100)


def test_explicit_bidi_spoofing_characters_are_removed():
    assert clean_text("user\u202egnp.exe", 100) == "usergnp.exe"
    assert clean_text("\u2066abc\u2069", 100) == "abc"
    assert clean_text("a\u200fb", 100) == "ab"


def test_truncates_to_limit():
    assert clean_text("abcdef", 4) == "abcd"
    assert clean_text("中" * 50, 10) == "中" * 10
    assert len(clean_name("x" * 200, limit=40)) == 40


def test_non_string_returns_default():
    for value in (None, 123, 1.5, b"text", ["a"], {"a": 1}):
        assert clean_text(value, 10) == ""
        assert clean_name(value) == "未命名设备"
        assert clean_name(value, default="X") == "X"


def test_empty_or_whitespace_uses_default():
    assert clean_name("") == "未命名设备"
    assert clean_name("   \t\n\u202e ") == "未命名设备"
    assert clean_name("", default="设备") == "设备"


def test_keeps_chinese_and_emoji():
    assert clean_text("中文😀👍🏽", 40) == "中文😀👍🏽"
    assert clean_name("中文😀") == "中文😀"


@pytest.mark.parametrize("value", ["中文", "😀", "abc😀中文"])
def test_keeps_printable_text(value):
    assert clean_text(value, 100) == value
