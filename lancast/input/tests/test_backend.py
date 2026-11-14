"""Fake backend behaviour plus construction-only checks for the real backend.

No test in this package calls the real ``SendInput``: the only real-backend
operations here are construction and ``cursor_position()``.
"""

from __future__ import annotations

import logging
import os

import pytest

import lancast.input.sendinput as sendinput_module
from lancast.input import (
    FakeInputBackend,
    InputBackend,
    SendInputBackend,
    create_input_backend,
)


def test_fake_backend_records_calls_and_tracks_cursor():
    backend = FakeInputBackend()
    assert backend.name == "fake"
    assert isinstance(backend, InputBackend)
    assert backend.cursor_position() == (0, 0)

    backend.move_relative(3, -2)
    assert backend.cursor_position() == (3, -2)

    backend.move_absolute(100, -50)
    assert backend.cursor_position() == (100, -50)

    backend.button("x1", True)
    backend.wheel(-120)
    backend.wheel(240, 120)
    backend.key("KeyW", True)
    backend.text("你好")

    assert backend.calls == [
        ("cursor_position",),
        ("move_relative", 3, -2),
        ("cursor_position",),
        ("move_absolute", 100, -50),
        ("cursor_position",),
        ("button", "x1", True),
        ("wheel", -120, 0),
        ("wheel", 240, 120),
        ("key", "KeyW", True),
        ("text", "你好"),
    ]


def test_fake_backend_rejects_unknown_keys_and_buttons():
    backend = FakeInputBackend()
    with pytest.raises(ValueError):
        backend.key("KeyAA", True)
    with pytest.raises(ValueError):
        backend.button("wheel", True)
    with pytest.raises(ValueError):
        backend.text(None)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        backend.move_relative("left", 1)  # type: ignore[arg-type]
    assert backend.calls == []


def test_fake_backend_accepts_every_documented_key():
    backend = FakeInputBackend()
    from lancast.input import KEY_CODES

    for code in KEY_CODES:
        backend.key(code, True)
        backend.key(code, False)
    assert len(backend.calls) == 2 * len(KEY_CODES)


def test_create_input_backend_fake():
    backend = create_input_backend(fake=True)
    assert isinstance(backend, FakeInputBackend)
    assert backend.name == "fake"


@pytest.mark.skipif(os.name != "nt", reason="SendInput backend is Windows-only")
def test_real_backend_can_be_constructed_and_reads_cursor():
    backend = create_input_backend()
    assert isinstance(backend, SendInputBackend)
    assert backend.name == "sendinput"
    x, y = backend.cursor_position()
    assert isinstance(x, int) and isinstance(y, int)


@pytest.mark.skipif(os.name != "nt", reason="SendInput backend is Windows-only")
def test_real_backend_calls_dpi_awareness_before_creation(monkeypatch):
    called: list[bool] = []
    monkeypatch.setattr(
        sendinput_module, "_ensure_dpi_awareness", lambda: called.append(True)
    )
    SendInputBackend()
    assert called == [True]


@pytest.mark.skipif(os.name != "nt", reason="SendInput backend is Windows-only")
def test_text_is_submitted_in_one_batch(monkeypatch):
    backend = SendInputBackend()
    batches: list[list] = []

    def fake_send(events):
        batches.append(events)
        return len(events)

    monkeypatch.setattr(sendinput_module, "_send_inputs", fake_send)
    backend.text("a你😀")
    assert len(batches) == 1
    assert len(batches[0]) == 8


def test_partial_sendinput_warning_is_rate_limited(monkeypatch, caplog):
    monkeypatch.setattr(sendinput_module, "_last_warning_at", None)
    caplog.set_level(logging.WARNING, logger="lancast.input")

    sendinput_module._warn_partial(1, 2)
    sendinput_module._warn_partial(1, 2)
    assert len(caplog.records) == 1

    # Pretend ten seconds passed: the next failure warns again.
    sendinput_module._last_warning_at = sendinput_module.time.monotonic() - 11
    sendinput_module._warn_partial(1, 2)
    assert len(caplog.records) == 2
