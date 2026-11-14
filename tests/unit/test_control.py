"""Unit tests for the remote-control policy layer (lancast/control.py)."""

from __future__ import annotations

from types import SimpleNamespace

from lancast.control import (
    FLOOD_CLOSE_AFTER,
    INPUT_BURST,
    MAX_DELTA,
    MAX_EVENTS,
    MAX_WHEEL,
    TEXT_CHARS_PER_S,
    WATCHDOG_S,
    InputController,
    clean_typed_text,
)
from lancast.input import BUTTONS, KEY_CODES, FakeInputBackend


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def make(display=None):
    clock = Clock()
    backend = FakeInputBackend()
    disp = display or SimpleNamespace(x=-1920, y=0, width=1920, height=1080)
    ctl = InputController(backend, KEY_CODES, BUTTONS, lambda: disp, clock=clock)
    return ctl, backend, clock


def test_relative_moves_accumulate_subpixels_and_clamp():
    ctl, backend, _ = make()
    ctl.handle([{"t": "mm", "dx": 0.4, "dy": -0.4}, {"t": "mm", "dx": 0.4, "dy": -0.4},
                {"t": "mm", "dx": 0.4, "dy": -0.4}])
    assert backend.calls == [("move_relative", 1, -1)]
    backend.calls.clear()
    ctl.handle([{"t": "mm", "dx": 1e9, "dy": -1e9}])
    assert backend.calls == [("move_relative", int(MAX_DELTA), -int(MAX_DELTA))]


def test_absolute_positions_map_onto_the_watched_display():
    ctl, backend, _ = make()
    ctl.handle([{"t": "ma", "x": 0, "y": 0}, {"t": "ma", "x": 1, "y": 1}, {"t": "ma", "x": 7, "y": -3}])
    assert backend.calls == [("move_absolute", -1920, 0), ("move_absolute", -1, 1079), ("move_absolute", -1, 0)]


def test_absolute_ignored_without_display():
    backend = FakeInputBackend()
    ctl = InputController(backend, KEY_CODES, BUTTONS, lambda: None)
    assert ctl.handle([{"t": "ma", "x": 0.5, "y": 0.5}]) == 0
    assert backend.calls == []


def test_invalid_events_are_rejected_not_raised():
    ctl, backend, _ = make()
    bad = [None, 3, "x", {}, {"t": "zz"}, {"t": "mm", "dx": True, "dy": 0}, {"t": "mm", "dx": "1", "dy": 0},
           {"t": "mm", "dx": float("inf"), "dy": 0}, {"t": "mb", "b": "left", "d": 1},
           {"t": "kd", "k": "keya"}, {"t": "tx", "s": 5}, {"t": "mw", "dy": float("nan")}]
    assert ctl.handle(bad) == 0
    assert ctl.rejected == len(bad)
    assert backend.calls == []
    assert ctl.handle("not a list") == 0


def test_event_count_limit_per_message():
    ctl, backend, _ = make()
    applied = ctl.handle([{"t": "mw", "dy": 1}] * (MAX_EVENTS + 10))
    assert applied == MAX_EVENTS and len(backend.calls) == MAX_EVENTS
    assert ctl.rejected == 10


def test_wheel_clamped_and_zero_skipped():
    ctl, backend, _ = make()
    ctl.handle([{"t": "mw", "dy": 99999, "dx": -99999}, {"t": "mw", "dy": 0}])
    assert backend.calls == [("wheel", MAX_WHEEL, -MAX_WHEEL)]


def test_keys_track_state_and_release_in_reverse_order():
    ctl, backend, _ = make()
    ctl.handle([{"t": "kd", "k": "ControlLeft"}, {"t": "kd", "k": "ShiftLeft"}, {"t": "kd", "k": "Escape"},
                {"t": "kd", "k": "Escape"}, {"t": "mb", "b": "left", "d": True}, {"t": "mb", "b": "left", "d": True}])
    assert ctl.held_keys == ["ControlLeft", "ShiftLeft", "Escape"] and ctl.held_buttons == ["left"]
    backend.calls.clear()
    ctl.release_all()
    assert backend.calls == [("key", "Escape", False), ("key", "ShiftLeft", False), ("key", "ControlLeft", False),
                             ("button", "left", False)]
    assert not ctl.holding()
    # releasing something that is not held does nothing
    backend.calls.clear()
    ctl.handle([{"t": "ku", "k": "KeyA"}, {"t": "mb", "b": "right", "d": False}])
    assert backend.calls == []


def test_watchdog_releases_after_silence_only_when_holding():
    ctl, backend, clock = make()
    assert ctl.watchdog() is False
    ctl.handle([{"t": "kd", "k": "KeyW"}])
    clock.t += WATCHDOG_S - 0.1
    assert ctl.watchdog() is False
    ctl.handle([])                       # heartbeat keeps the key held
    clock.t += WATCHDOG_S - 0.1
    assert ctl.watchdog() is False
    clock.t += 0.2
    assert ctl.watchdog() is True
    assert backend.calls[-1] == ("key", "KeyW", False)


def test_message_rate_limit_and_flood_detection():
    ctl, _, clock = make()
    allowed = sum(ctl.allow_message() for _ in range(INPUT_BURST))
    assert allowed == INPUT_BURST
    assert ctl.allow_message() is False
    for _ in range(FLOOD_CLOSE_AFTER + 1):
        ctl.allow_message()
    assert ctl.flooding()
    clock.t += 1.0                       # tokens refill; a successful message resets the streak
    assert ctl.allow_message() is True and not ctl.flooding()


def test_text_rate_limit_and_cleaning():
    ctl, backend, clock = make()
    chunk = "字" * 200
    sent = sum(ctl.handle([{"t": "tx", "s": chunk}]) for _ in range(20))
    assert sent == TEXT_CHARS_PER_S // 200
    clock.t += 1.0
    assert ctl.handle([{"t": "tx", "s": "ok"}]) == 1
    assert clean_typed_text("a\r\nb\rc\td\x00e\x1bf\u0085g") == "a\nb\nc\tdefg"
    assert len(clean_typed_text("x" * 1000)) == 200


def test_active_flag_follows_applied_events():
    ctl, _, clock = make()
    assert ctl.active() is False
    ctl.handle([])                       # heartbeat is not activity
    assert ctl.active() is False
    ctl.handle([{"t": "mm", "dx": 5, "dy": 0}])
    assert ctl.active() is True
    clock.t += 3.5
    assert ctl.active() is False
