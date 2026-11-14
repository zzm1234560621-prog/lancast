"""Cursor state tests: types, ranges, thread safety and latency."""

from __future__ import annotations

import statistics
import threading
import time

from lancast.capture import CursorState, DisplayInfo, get_cursor

_ALLOWED_SHAPES = {
    "arrow",
    "ibeam",
    "hand",
    "wait",
    "cross",
    "move",
    "size_ns",
    "size_we",
    "size_nwse",
    "size_nesw",
    "no",
    "other",
}

_DISPLAY = DisplayInfo(
    index=0,
    id=r"\\.\DISPLAY1",
    name="Display 1",
    x=0,
    y=0,
    width=1920,
    height=1080,
    primary=True,
)


def test_get_cursor_type_and_range():
    state = get_cursor(_DISPLAY)
    assert isinstance(state, CursorState)
    assert isinstance(state.visible, bool)
    assert 0.0 <= state.x <= 1.0
    assert 0.0 <= state.y <= 1.0
    assert state.shape in _ALLOWED_SHAPES


def test_get_cursor_is_not_visible_on_a_far_display():
    far = DisplayInfo(
        index=1,
        id=r"\\.\DISPLAY99",
        name="Display 99",
        x=1_000_000,
        y=1_000_000,
        width=640,
        height=480,
        primary=False,
    )
    state = get_cursor(far)
    assert state.visible is False
    assert 0.0 <= state.x <= 1.0
    assert 0.0 <= state.y <= 1.0
    assert state.shape in _ALLOWED_SHAPES


def test_get_cursor_is_fast():
    samples = []
    for _ in range(200):
        start = time.perf_counter_ns()
        get_cursor(_DISPLAY)
        samples.append(time.perf_counter_ns() - start)
    median_ms = statistics.median(samples) / 1_000_000
    assert median_ms < 1.0, f"median get_cursor() took {median_ms:.3f} ms"


def test_get_cursor_is_thread_safe():
    errors: list[BaseException] = []

    def worker():
        try:
            for _ in range(100):
                state = get_cursor(_DISPLAY)
                assert isinstance(state, CursorState)
                assert 0.0 <= state.x <= 1.0
                assert 0.0 <= state.y <= 1.0
                assert state.shape in _ALLOWED_SHAPES
        except BaseException as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not errors, errors
