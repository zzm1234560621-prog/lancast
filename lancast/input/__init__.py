"""Remote-control input injection for LanCast.

The public contract is documented in ``docs/ARCHITECTURE.md``; event
validation, permissions, limits and stuck-key tracking live in
``lancast/control.py``.  This package only translates already-validated
events into OS input.

On Windows :class:`SendInputBackend` uses ``user32.SendInput`` in physical
pixels; everywhere else (and with ``fake=True``) :class:`FakeInputBackend`
records calls without touching the real mouse or keyboard.
"""

from __future__ import annotations

import os

from .backend import FakeInputBackend, InputBackend
from .keycodes import BUTTONS, KEY_CODES, key_info
from .sendinput import SendInputBackend

__all__ = [
    "BUTTONS",
    "KEY_CODES",
    "FakeInputBackend",
    "InputBackend",
    "SendInputBackend",
    "create_input_backend",
    "key_info",
]


def create_input_backend(fake: bool = False) -> InputBackend:
    """Return the SendInput backend on Windows, otherwise a fake backend.

    ``fake=True`` always selects :class:`FakeInputBackend`, which is also the
    only option on non-Windows platforms.
    """

    if not fake and os.name == "nt":
        return SendInputBackend()
    return FakeInputBackend()
