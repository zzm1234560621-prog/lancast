"""Shared dataclasses and the capture-source protocol.

The public contract lives in ``docs/ARCHITECTURE.md``; keep this module in
sync with it.  Everything here is platform independent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import numpy as np


@dataclass(frozen=True)
class DisplayInfo:
    """One physical display in the Windows virtual desktop."""

    index: int
    """Position in :func:`lancast.capture.list_displays` (0 = primary)."""

    id: str
    """OS device name, e.g. ``r"\\.\DISPLAY1"``."""

    name: str
    """Human readable label, e.g. ``"Display 1"``."""

    x: int
    """Left edge in virtual-desktop physical pixels."""

    y: int
    """Top edge in virtual-desktop physical pixels."""

    width: int
    """Native physical width, rotation applied."""

    height: int
    """Native physical height, rotation applied."""

    primary: bool


@dataclass(frozen=True)
class CursorState:
    """Mouse pointer state normalised to one display."""

    x: float
    y: float
    visible: bool
    shape: str


class CaptureError(Exception):
    """A capture backend could not be created or the display is gone for good."""


@runtime_checkable
class CaptureSource(Protocol):
    """Protocol implemented by every capture backend.

    A source is created, used and closed on one pipeline thread.  ``grab``
    must never block for a whole frame and must return an owned array.
    """

    display: DisplayInfo
    backend: str

    def grab(self) -> np.ndarray | None:
        """Return the next BGRA frame, or ``None`` when nothing changed."""

    def close(self) -> None:
        """Release backend resources.  Safe to call more than once."""
