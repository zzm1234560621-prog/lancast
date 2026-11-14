"""GDI / MSS capture backend.

MSS is created lazily inside the thread that calls :meth:`grab`, because an
``mss.mss()`` instance owns thread-affine GDI objects.  The backend is the
portable fallback when DXGI desktop duplication is unavailable (secure
desktop, remote session, virtual display driver, ...).

A cheap sampled-frame comparison suppresses duplicate frames while the
screen is static; the first frame after opening is always returned.
"""

from __future__ import annotations

import logging
import threading
import time

import numpy as np

from .base import CaptureError, DisplayInfo

logger = logging.getLogger("lancast.capture")

_FAILURE_GRACE_S = 3.0
_CHANGE_STRIDE = 8


class MssCapture:
    """Capture one display through MSS (BitBlt)."""

    backend = "mss"

    def __init__(self, display: DisplayInfo) -> None:
        self.display = display
        self._region = {
            "left": int(display.x),
            "top": int(display.y),
            "width": int(display.width),
            "height": int(display.height),
        }
        self._local = threading.local()
        self._scts: list[object] = []
        self._lock = threading.Lock()
        self._closed = False
        self._fail_since: float | None = None
        self._last_error_log = 0.0
        self._last_sample: bytes | None = None

    def _sct_for_thread(self):
        sct = getattr(self._local, "sct", None)
        if sct is None:
            import mss  # lazy: only needed when this backend is used

            # mss >= 10 names the class MSS and deprecates the mss() factory.
            sct = getattr(mss, "MSS", mss.mss)()
            self._local.sct = sct
            with self._lock:
                if self._closed:
                    try:
                        sct.close()
                    finally:
                        raise CaptureError("MSS capture source is closed")
                self._scts.append(sct)
        return sct

    def _drop_thread_sct(self) -> None:
        sct = getattr(self._local, "sct", None)
        self._local.sct = None
        if sct is None:
            return
        with self._lock:
            try:
                self._scts.remove(sct)
            except ValueError:
                pass
        try:
            sct.close()
        except Exception:
            logger.debug("Ignoring error while discarding MSS instance.", exc_info=True)

    def _reload_display(self) -> bool:
        """Refresh geometry after a mode change.  Returns True when it changed."""

        from .displays import list_displays

        try:
            displays = list_displays()
        except Exception:
            logger.debug("Could not re-enumerate displays.", exc_info=True)
            return False
        wanted = self.display.id.casefold()
        for display in displays:
            if display.id.casefold() != wanted:
                continue
            geometry = (display.x, display.y, display.width, display.height)
            previous = (
                self.display.x,
                self.display.y,
                self.display.width,
                self.display.height,
            )
            if geometry == previous:
                return False
            self.display = display
            self._region = {
                "left": int(display.x),
                "top": int(display.y),
                "width": int(display.width),
                "height": int(display.height),
            }
            self._last_sample = None
            return True
        return False

    def _note_failure(self, exc: BaseException) -> None:
        now = time.monotonic()
        if self._fail_since is None:
            self._fail_since = now
        if now - self._last_error_log >= 1.0:
            self._last_error_log = now
            logger.warning("MSS capture failed: %s", exc)
        self._drop_thread_sct()

        # A mode switch changes the region we must BitBlt.  Re-read it from
        # Win32; the next grab will rebuild the MSS instance and use it.
        if self._reload_display():
            self._fail_since = None
            logger.info(
                "MSS capture geometry updated to %s (%dx%d at %d,%d).",
                self.display.id,
                self.display.width,
                self.display.height,
                self.display.x,
                self.display.y,
            )
            return

        if now - self._fail_since > _FAILURE_GRACE_S:
            raise CaptureError(
                f"MSS capture for {self.display.id} failed for more than "
                f"{_FAILURE_GRACE_S:.0f}s"
            ) from exc

    def grab(self) -> np.ndarray | None:
        if self._closed:
            return None

        try:
            sct = self._sct_for_thread()
            shot = sct.grab(self._region)
            raw = shot.raw
            height = self.display.height
            width = self.display.width
            # One copy: frombuffer() views the bytearray, .copy() gives the
            # caller an owned, C-contiguous BGRA array.
            frame = np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 4).copy()
        except CaptureError:
            raise
        except Exception as exc:
            self._note_failure(exc)
            return None

        self._fail_since = None

        # Cheap duplicate suppression: sample every Nth pixel instead of
        # comparing 20 MB of BGRA on a 2880x1800 screen.
        sample = frame[::_CHANGE_STRIDE, ::_CHANGE_STRIDE].tobytes()
        if self._last_sample == sample:
            return None
        self._last_sample = sample
        return frame

    def close(self) -> None:
        self._closed = True
        with self._lock:
            scts = self._scts
            self._scts = []
        for sct in scts:
            try:
                sct.close()
            except Exception:
                logger.debug("Ignoring error while closing MSS instance.", exc_info=True)
        self._last_sample = None
