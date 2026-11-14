"""Remote-control policy: validation, limits, held-input tracking and the watchdog.

Every event a viewer sends passes through :class:`InputController` before
anything reaches the operating system (``lancast.input``). See
docs/PROTOCOL.md §3.7 and docs/SECURITY.md.

Typed text and key codes are never logged: they may contain passwords.
"""

from __future__ import annotations

import math
import time
import unicodedata
from typing import Callable

MAX_EVENTS = 64                 # events per input message
MAX_DELTA = 4000.0              # |dx|, |dy| per relative move
MAX_WHEEL = 2400                # |wheel| per event (20 notches)
MAX_TEXT_EVENT = 200            # characters per tx event
TEXT_CHARS_PER_S = 2000
INPUT_BURST, INPUT_PER_S = 240, 120.0
WATCHDOG_S = 3.0                # release held input after this long without any input message
ACTIVE_S = 3.0                  # "controlling" indicator for the admin page
FLOOD_CLOSE_AFTER = 400         # consecutive rate-limited messages before the server disconnects


def _num(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    return f if math.isfinite(f) else None


def _clamp(value: float, limit: float) -> float:
    return max(-limit, min(limit, value))


def clean_typed_text(s: str) -> str:
    """Normalise line breaks and drop control characters other than newline and tab."""
    s = s.replace("\r\n", "\n").replace("\r", "\n")[:MAX_TEXT_EVENT]
    return "".join(ch for ch in s if ch in "\n\t" or unicodedata.category(ch) != "Cc")


class _Bucket:
    def __init__(self, capacity: float, per_s: float, clock: Callable[[], float]) -> None:
        self.capacity = capacity
        self.tokens = capacity
        self.per_s = per_s
        self.clock = clock
        self.t = clock()

    def take(self, n: float = 1.0) -> bool:
        now = self.clock()
        self.tokens = min(self.capacity, self.tokens + (now - self.t) * self.per_s)
        self.t = now
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False


class InputController:
    """Validates one viewer's input events and drives an ``InputBackend``.

    ``display`` returns the DisplayInfo of the display the viewer is watching
    (or None); absolute positions are mapped onto it.
    """

    def __init__(
        self,
        backend,
        key_codes,
        buttons,
        display: Callable[[], object | None],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.backend = backend
        self.key_codes = frozenset(key_codes)
        self.buttons = frozenset(buttons)
        self._display = display
        self._clock = clock
        self.held_keys: list[str] = []       # press order; released in reverse
        self.held_buttons: list[str] = []
        self._fx = 0.0
        self._fy = 0.0
        self.last_message = 0.0
        self.last_active = 0.0
        self.applied = 0
        self.rejected = 0
        self.limited_in_a_row = 0
        self._messages = _Bucket(INPUT_BURST, INPUT_PER_S, clock)
        self._text = _Bucket(TEXT_CHARS_PER_S, TEXT_CHARS_PER_S, clock)

    # -- message level -------------------------------------------------------------
    def allow_message(self) -> bool:
        if self._messages.take():
            self.limited_in_a_row = 0
            return True
        self.limited_in_a_row += 1
        return False

    def flooding(self) -> bool:
        return self.limited_in_a_row > FLOOD_CLOSE_AFTER

    def handle(self, events) -> int:
        """Apply a batch of events; returns how many were applied."""
        now = self._clock()
        self.last_message = now
        if not isinstance(events, list):
            self.rejected += 1
            return 0
        applied = 0
        for event in events[:MAX_EVENTS]:
            if self._apply(event):
                applied += 1
            else:
                self.rejected += 1
        self.rejected += max(0, len(events) - MAX_EVENTS)
        if applied:
            self.last_active = now
            self.applied += applied
        return applied

    # -- single event ----------------------------------------------------------------
    def _apply(self, event) -> bool:
        if not isinstance(event, dict):
            return False
        kind = event.get("t")
        backend = self.backend
        try:
            if kind == "mm":
                dx, dy = _num(event.get("dx")), _num(event.get("dy"))
                if dx is None or dy is None:
                    return False
                self._fx += _clamp(dx, MAX_DELTA)
                self._fy += _clamp(dy, MAX_DELTA)
                ix, iy = int(self._fx), int(self._fy)    # sub-pixel remainder carries over
                self._fx -= ix
                self._fy -= iy
                if ix or iy:
                    backend.move_relative(ix, iy)
                return True
            if kind == "ma":
                x, y = _num(event.get("x")), _num(event.get("y"))
                disp = self._display()
                if x is None or y is None or disp is None:
                    return False
                x = min(max(x, 0.0), 1.0)
                y = min(max(y, 0.0), 1.0)
                backend.move_absolute(disp.x + round(x * (disp.width - 1)), disp.y + round(y * (disp.height - 1)))
                return True
            if kind == "mb":
                button, down = event.get("b"), event.get("d")
                if button not in self.buttons or not isinstance(down, bool):
                    return False
                if down and button not in self.held_buttons:
                    backend.button(button, True)
                    self.held_buttons.append(button)
                elif not down and button in self.held_buttons:
                    backend.button(button, False)
                    self.held_buttons.remove(button)
                return True
            if kind == "mw":
                dy, dx = _num(event.get("dy", 0)), _num(event.get("dx", 0))
                if dy is None or dx is None:
                    return False
                dy, dx = int(_clamp(dy, MAX_WHEEL)), int(_clamp(dx, MAX_WHEEL))
                if dy or dx:
                    backend.wheel(dy, dx)
                return True
            if kind in ("kd", "ku"):
                code = event.get("k")
                if not isinstance(code, str) or code not in self.key_codes:
                    return False
                if kind == "kd" and code not in self.held_keys:     # repeats are ignored
                    backend.key(code, True)
                    self.held_keys.append(code)
                elif kind == "ku" and code in self.held_keys:
                    backend.key(code, False)
                    self.held_keys.remove(code)
                return True
            if kind == "tx":
                s = event.get("s")
                if not isinstance(s, str):
                    return False
                s = clean_typed_text(s)
                if not s:
                    return True
                if not self._text.take(len(s)):
                    return False
                backend.text(s)
                return True
        except ValueError:
            return False
        return False

    # -- safety ------------------------------------------------------------------------
    def holding(self) -> bool:
        return bool(self.held_keys or self.held_buttons)

    def release_all(self) -> None:
        """Let go of every key and button this viewer holds (reverse press order)."""
        for code in reversed(self.held_keys):
            try:
                self.backend.key(code, False)
            except Exception:  # noqa: BLE001 - releasing must never raise
                pass
        for button in reversed(self.held_buttons):
            try:
                self.backend.button(button, False)
            except Exception:  # noqa: BLE001
                pass
        self.held_keys.clear()
        self.held_buttons.clear()
        self._fx = self._fy = 0.0

    def watchdog(self, now: float | None = None) -> bool:
        """Release held input if the viewer went silent. Returns True if it did."""
        now = self._clock() if now is None else now
        if self.holding() and now - self.last_message > WATCHDOG_S:
            self.release_all()
            return True
        return False

    def active(self, now: float | None = None) -> bool:
        now = self._clock() if now is None else now
        return self.last_active > 0 and now - self.last_active < ACTIVE_S
