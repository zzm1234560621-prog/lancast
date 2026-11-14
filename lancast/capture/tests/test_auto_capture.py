"""Real capture backend smoke tests (Windows only)."""

from __future__ import annotations

import os
import time

import numpy as np
import pytest

from lancast.capture import CaptureError, open_capture

pytestmark = pytest.mark.skipif(
    os.name != "nt", reason="real capture requires Windows"
)


def _first_frame(source, timeout_s: float = 1.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        frame = source.grab()
        if frame is not None:
            return frame
        time.sleep(0.005)
    return None


def _check_frame(frame, source):
    assert isinstance(frame, np.ndarray)
    assert frame.dtype == np.uint8
    assert frame.shape == (source.display.height, source.display.width, 4)
    assert frame.flags.c_contiguous


def test_open_capture_auto_returns_frame_within_one_second():
    source = open_capture(0, "auto")
    used_backend = source.backend
    frame = _first_frame(source)
    try:
        if frame is None and used_backend == "dxgi":
            # The desktop may be completely static right after duplication
            # starts.  Retry through MSS so the test still proves that a
            # usable real capture path exists on this machine.
            source.close()
            source = open_capture(0, "mss")
            used_backend = source.backend
            frame = _first_frame(source)
        assert frame is not None, (
            f"no frame from backend {used_backend!r} within 1 s"
        )
        _check_frame(frame, source)
    finally:
        source.close()


def test_mss_backend_returns_frame():
    source = open_capture(0, "mss")
    try:
        frame = _first_frame(source)
        assert frame is not None, "MSS returned no frame within 1 s"
        _check_frame(frame, source)
    finally:
        source.close()


def test_dxgi_can_be_reopened_after_close():
    try:
        source = open_capture(0, "dxgi")
    except CaptureError as exc:
        pytest.skip(f"DXGI is unavailable here: {exc}")

    try:
        frame = _first_frame(source)
        assert frame is not None, "first DXGI session returned no frame within 1 s"
        _check_frame(frame, source)

        # dxcam caches one camera per output; close() must release it so the
        # next open() gets a usable instance instead of the released one.
        source.close()
        source = open_capture(0, "dxgi")
        frame = _first_frame(source)
        assert frame is not None, "reopened DXGI session returned no frame within 1 s"
        _check_frame(frame, source)
    finally:
        source.close()
