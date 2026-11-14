"""DXGI Desktop Duplication backend built on ``dxcam``.

``dxcam`` exposes adapters and their outputs as separate indices, while the
rest of LanCast works with Win32 ``DisplayInfo`` objects.  The mapping is
done by DXGI output device name (``\\\\.\\DISPLAY1``), falling back to
desktop coordinates plus resolution when a name is unavailable.

One ``dxcam`` camera exists per ``(adapter, output, backend)`` tuple and is
weakly cached by the library.  ``close()`` releases it; the next
``dxcam.create`` observes ``is_released`` and builds a fresh camera.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

import numpy as np

from .base import CaptureError, DisplayInfo

logger = logging.getLogger("lancast.capture")

_FAILURE_GRACE_S = 3.0
_REOPEN_INTERVAL_S = 0.5


@dataclass(frozen=True)
class _DxcamOutput:
    device_idx: int
    output_idx: int
    device_name: str
    left: int
    top: int
    width: int
    height: int


def _catalog_from_factory(dxcam_module) -> list[_DxcamOutput]:
    """Read the outputs already enumerated by dxcam's module-level factory."""

    factory = getattr(dxcam_module, "__factory", None)
    if factory is None:
        return []
    entries: list[_DxcamOutput] = []
    outputs_per_device = getattr(factory, "outputs", None) or []
    for device_idx, outputs in enumerate(outputs_per_device):
        for output_idx, output in enumerate(outputs):
            try:
                output.update_desc()
                desc = output.desc
                rect = desc.DesktopCoordinates
                entries.append(
                    _DxcamOutput(
                        device_idx=device_idx,
                        output_idx=output_idx,
                        device_name=str(output.devicename),
                        left=int(rect.left),
                        top=int(rect.top),
                        width=int(rect.right) - int(rect.left),
                        height=int(rect.bottom) - int(rect.top),
                    )
                )
            except Exception:
                logger.debug(
                    "Could not inspect dxcam output %s/%s.",
                    device_idx,
                    output_idx,
                    exc_info=True,
                )
    return entries


def _catalog_by_enumeration(dxcam_module) -> list[_DxcamOutput]:
    """Fallback: enumerate adapters ourselves through dxcam internals."""

    try:
        from dxcam.core import Device, Output
        from dxcam.util.io import enum_dxgi_adapters
    except Exception:  # pragma: no cover - dxcam version differences
        logger.debug("dxcam enumeration helpers are unavailable.", exc_info=True)
        return []

    entries: list[_DxcamOutput] = []
    for device_idx, adapter in enumerate(enum_dxgi_adapters()):
        try:
            device = Device(adapter)
            outputs = device.enum_outputs()
        except Exception:
            logger.debug("Could not enumerate dxcam adapter %s.", device_idx, exc_info=True)
            continue
        for output_idx, raw_output in enumerate(outputs):
            try:
                output = Output(raw_output)
                desc = output.desc
                rect = desc.DesktopCoordinates
                entries.append(
                    _DxcamOutput(
                        device_idx=device_idx,
                        output_idx=output_idx,
                        device_name=str(output.devicename),
                        left=int(rect.left),
                        top=int(rect.top),
                        width=int(rect.right) - int(rect.left),
                        height=int(rect.bottom) - int(rect.top),
                    )
                )
            except Exception:
                logger.debug(
                    "Could not inspect enumerated output %s/%s.",
                    device_idx,
                    output_idx,
                    exc_info=True,
                )
    return entries


def dxcam_output_catalog() -> list[_DxcamOutput]:
    """Return all DXGI outputs with their dxcam indices and desktop geometry."""

    import dxcam

    entries = _catalog_from_factory(dxcam)
    if not entries:
        entries = _catalog_by_enumeration(dxcam)
    return entries


def map_display_to_dxcam(display: DisplayInfo) -> tuple[int, int]:
    """Map a :class:`DisplayInfo` to ``dxcam.create``'s indices.

    Matching order: exact OS device name, then desktop position+resolution,
    then a unique resolution match.
    """

    catalog = dxcam_output_catalog()
    if not catalog:
        raise CaptureError("dxcam reported no DXGI outputs")

    wanted_name = display.id.casefold()
    name_matches = [e for e in catalog if e.device_name.casefold() == wanted_name]
    if name_matches:
        chosen = name_matches[0]
        return chosen.device_idx, chosen.output_idx

    geometry_matches = [
        e
        for e in catalog
        if e.left == display.x
        and e.top == display.y
        and e.width == display.width
        and e.height == display.height
    ]
    if geometry_matches:
        chosen = geometry_matches[0]
        return chosen.device_idx, chosen.output_idx

    size_matches = [
        e for e in catalog if e.width == display.width and e.height == display.height
    ]
    if len(size_matches) == 1:
        chosen = size_matches[0]
        return chosen.device_idx, chosen.output_idx

    available = ", ".join(
        f"{e.device_name}@{e.left},{e.top} {e.width}x{e.height}" for e in catalog
    )
    raise CaptureError(
        f"Display {display.id!r} ({display.width}x{display.height} at "
        f"{display.x},{display.y}) is not a DXGI output; available: {available}"
    )


class DxgiCapture:
    """DXGI desktop duplication for one display, with automatic recovery."""

    backend = "dxgi"

    def __init__(self, display: DisplayInfo) -> None:
        self.display = display
        self._cam = None
        self._lock = threading.RLock()
        self._closed = False
        self._fail_since: float | None = None
        self._last_reopen = 0.0
        self._last_error_log = 0.0
        self._open_camera()

    def _open_camera(self) -> None:
        import dxcam

        device_idx, output_idx = map_display_to_dxcam(self.display)
        camera = dxcam.create(
            device_idx=device_idx,
            output_idx=output_idx,
            output_color="BGRA",
        )
        if getattr(camera, "channel_size", 4) != 4:
            try:
                camera.release()
            finally:
                raise CaptureError("dxcam did not create a BGRA camera")
        self._cam = camera
        logger.debug(
            "DXGI capture ready for %s via adapter=%s output=%s.",
            self.display.id,
            device_idx,
            output_idx,
        )

    def _release_camera(self) -> None:
        camera = self._cam
        self._cam = None
        if camera is None:
            return
        try:
            camera.release()
        except Exception:
            logger.debug("Ignoring error while releasing DXGI camera.", exc_info=True)

    def _note_failure(self, exc: BaseException) -> None:
        now = time.monotonic()
        if self._fail_since is None:
            self._fail_since = now
        if now - self._last_error_log >= 1.0:
            self._last_error_log = now
            logger.warning("DXGI capture failed: %s", exc)

        self._release_camera()

        if now - self._fail_since > _FAILURE_GRACE_S:
            raise CaptureError(
                f"DXGI capture for {self.display.id} failed for more than "
                f"{_FAILURE_GRACE_S:.0f}s"
            ) from exc

        # Re-create at most twice a second.  dxcam.create() deliberately
        # sleeps 100 ms after creation, so re-opening on every call would
        # make a UAC transition far worse.
        if now - self._last_reopen >= _REOPEN_INTERVAL_S:
            self._last_reopen = now
            try:
                self._open_camera()
            except Exception as open_exc:
                logger.debug("DXGI re-open failed: %s", open_exc, exc_info=True)

    def grab(self) -> np.ndarray | None:
        if self._closed:
            return None

        with self._lock:
            camera = self._cam
            if camera is None:
                now = time.monotonic()
                if (
                    self._fail_since is not None
                    and now - self._fail_since > _FAILURE_GRACE_S
                ):
                    raise CaptureError(
                        f"DXGI capture for {self.display.id} is unavailable"
                    )
                if now - self._last_reopen >= _REOPEN_INTERVAL_S:
                    self._last_reopen = now
                    try:
                        self._open_camera()
                    except Exception as exc:
                        logger.debug("DXGI re-open failed: %s", exc, exc_info=True)
                return None

            try:
                frame = camera.grab()
            except Exception as exc:
                self._note_failure(exc)
                return None

            # A successful grab (even with no new frame) means the
            # duplication is healthy again.
            self._fail_since = None
            if frame is None:
                return None

            if (
                not isinstance(frame, np.ndarray)
                or frame.dtype != np.uint8
                or frame.ndim != 3
                or frame.shape[2] != 4
            ):
                raise CaptureError(
                    "DXGI returned an unexpected frame "
                    f"(type={type(frame)!r}, shape={getattr(frame, 'shape', None)}, "
                    f"dtype={getattr(frame, 'dtype', None)})"
                )

            # dxcam.grab(copy=True) already owns a fresh buffer.  Keep the
            # guarantee explicit in case a future version returns a view.
            if not frame.flags.c_contiguous or not frame.flags.owndata:
                frame = np.array(frame, dtype=np.uint8, order="C", copy=True)
            return frame

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._release_camera()
