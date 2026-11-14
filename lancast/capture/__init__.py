"""Screen capture backends and cursor state for LanCast.

The public contract is documented in ``docs/ARCHITECTURE.md``.  Importing
this package makes the process per-monitor DPI aware before any backend
touches display geometry or DXGI.
"""

from __future__ import annotations

import logging

from .dpi import ensure_dpi_awareness

# Must run before displays/dxcam/mss enumerate monitors or create devices.
ensure_dpi_awareness()

from .base import CaptureError, CaptureSource, CursorState, DisplayInfo
from .cursor import get_cursor
from .displays import list_displays
from .dxgi import DxgiCapture
from .mss_backend import MssCapture
from .synthetic import SyntheticCapture, open_synthetic

logger = logging.getLogger("lancast.capture")

__all__ = [
    "CaptureError",
    "CaptureSource",
    "CursorState",
    "DisplayInfo",
    "ensure_dpi_awareness",
    "get_cursor",
    "list_displays",
    "open_capture",
    "open_synthetic",
]

_VALID_BACKENDS = frozenset({"auto", "dxgi", "mss", "synthetic"})


def open_capture(display_index: int, backend: str = "auto") -> CaptureSource:
    """Open a capture source for ``display_index``.

    ``backend`` is ``"auto"`` (DXGI, falling back to MSS if initialisation
    fails), ``"dxgi"``, ``"mss"`` or ``"synthetic"``.  A synthetic source
    ignores the display index and uses a fake 1280x720 display.
    """

    name = str(backend).strip().lower()
    if name not in _VALID_BACKENDS:
        raise ValueError(
            f"Unknown capture backend {backend!r}; expected one of "
            f"{sorted(_VALID_BACKENDS)}"
        )

    if name == "synthetic":
        return open_synthetic(1280, 720, 60)

    displays = list_displays()
    if not (0 <= int(display_index) < len(displays)):
        raise CaptureError(
            f"Display index {display_index} is out of range "
            f"({len(displays)} display(s) found)"
        )
    display = displays[int(display_index)]

    if name == "dxgi":
        try:
            return DxgiCapture(display)
        except CaptureError:
            raise
        except Exception as exc:
            raise CaptureError(
                f"Could not initialise DXGI capture for {display.id}: {exc}"
            ) from exc

    if name == "mss":
        try:
            return MssCapture(display)
        except CaptureError:
            raise
        except Exception as exc:
            raise CaptureError(
                f"Could not initialise MSS capture for {display.id}: {exc}"
            ) from exc

    # "auto": DXGI is the low-latency path; MSS is the compatibility path.
    try:
        return DxgiCapture(display)
    except Exception as dxgi_exc:
        logger.warning(
            "DXGI capture unavailable for %s (%s); falling back to MSS.",
            display.id,
            dxgi_exc,
        )
        try:
            return MssCapture(display)
        except Exception as mss_exc:
            raise CaptureError(
                f"Both DXGI and MSS capture failed for {display.id}: "
                f"DXGI: {dxgi_exc}; MSS: {mss_exc}"
            ) from mss_exc
