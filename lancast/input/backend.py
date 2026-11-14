"""Input backend interface and the in-memory fake implementation."""

from __future__ import annotations

import abc

from .keycodes import BUTTONS, KEY_CODES

__all__ = ["InputBackend", "FakeInputBackend", "coerce_int"]


def coerce_int(value: object, what: str) -> int:
    """Convert an event coordinate to ``int``; raise ``ValueError`` if impossible."""

    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError(f"{what} must be an integer, got {value!r}") from None


def require_key_code(code: object) -> str:
    """Validate a key code, raising ``ValueError`` for unknown names."""

    if not isinstance(code, str) or code not in KEY_CODES:
        raise ValueError(f"Unknown key code {code!r}")
    return code


def require_button(button: object) -> str:
    """Validate a mouse button name, raising ``ValueError`` for unknown names."""

    if not isinstance(button, str) or button not in BUTTONS:
        raise ValueError(
            f"Unknown mouse button {button!r}; expected one of {BUTTONS}"
        )
    return button


class InputBackend(abc.ABC):
    """Turns validated remote-control events into OS input."""

    name: str

    @abc.abstractmethod
    def move_relative(self, dx: int, dy: int) -> None:
        """Move the pointer by ``(dx, dy)`` pixels."""

    @abc.abstractmethod
    def move_absolute(self, x: int, y: int) -> None:
        """Place the pointer at virtual-desktop physical pixel ``(x, y)``."""

    @abc.abstractmethod
    def button(self, button: str, down: bool) -> None:
        """Press or release a mouse button."""

    @abc.abstractmethod
    def wheel(self, dy: int, dx: int = 0) -> None:
        """Scroll by ``(dx, dy)`` in ``WHEEL_DELTA`` units."""

    @abc.abstractmethod
    def key(self, code: str, down: bool) -> None:
        """Press or release a keyboard key by ``KeyboardEvent.code``."""

    @abc.abstractmethod
    def text(self, s: str) -> None:
        """Type Unicode text."""

    @abc.abstractmethod
    def cursor_position(self) -> tuple[int, int]:
        """Return the pointer position in virtual-desktop physical pixels."""


class FakeInputBackend(InputBackend):
    """Records input calls instead of touching the real mouse and keyboard.

    Used by tests, ``--fake-input`` and non-Windows development.  The virtual
    cursor starts at ``(0, 0)``; :meth:`move_relative` accumulates and
    :meth:`move_absolute` assigns it.
    """

    name = "fake"

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.x = 0
        self.y = 0

    @property
    def position(self) -> tuple[int, int]:
        return self.x, self.y

    def move_relative(self, dx: int, dy: int) -> None:
        dx = coerce_int(dx, "dx")
        dy = coerce_int(dy, "dy")
        self.calls.append(("move_relative", dx, dy))
        self.x += dx
        self.y += dy

    def move_absolute(self, x: int, y: int) -> None:
        x = coerce_int(x, "x")
        y = coerce_int(y, "y")
        self.calls.append(("move_absolute", x, y))
        self.x = x
        self.y = y

    def button(self, button: str, down: bool) -> None:
        button = require_button(button)
        down = bool(down)
        self.calls.append(("button", button, down))

    def wheel(self, dy: int, dx: int = 0) -> None:
        dy = coerce_int(dy, "dy")
        dx = coerce_int(dx, "dx")
        self.calls.append(("wheel", dy, dx))

    def key(self, code: str, down: bool) -> None:
        code = require_key_code(code)
        down = bool(down)
        self.calls.append(("key", code, down))

    def text(self, s: str) -> None:
        if not isinstance(s, str):
            raise ValueError(f"text must be a string, got {type(s).__name__}")
        self.calls.append(("text", s))

    def cursor_position(self) -> tuple[int, int]:
        self.calls.append(("cursor_position",))
        return self.x, self.y
