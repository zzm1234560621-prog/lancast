"""Synthetic capture source tests: shape, ownership, throttling."""

from __future__ import annotations

import time

import numpy as np

from lancast.capture import DisplayInfo, open_capture, open_synthetic


def test_synthetic_shape_dtype_and_contiguity():
    source = open_synthetic(320, 180, 30)
    try:
        frame = source.grab()
        assert frame is not None
        assert frame.shape == (180, 320, 4)
        assert frame.dtype == np.uint8
        assert frame.flags.c_contiguous
        assert frame.flags.owndata
        assert source.backend == "synthetic"
        assert source.display.width == 320
        assert source.display.height == 180
    finally:
        source.close()


def test_synthetic_throttles_to_target_fps():
    # 5 fps => 200 ms between generated frames, so an immediate second call
    # must be throttled even on a slow machine.
    source = open_synthetic(160, 90, 5)
    try:
        first = source.grab()
        assert first is not None
        assert source.grab() is None

        time.sleep(0.25)
        second = source.grab()
        assert second is not None
        assert not np.array_equal(first, second), "animated pattern must change"

        # closed sources do not generate anything
        source.close()
        assert source.grab() is None
    finally:
        source.close()


def test_open_capture_synthetic_uses_fake_display():
    source = open_capture(99, "synthetic")
    try:
        assert source.backend == "synthetic"
        assert source.display == DisplayInfo(
            index=0,
            id="synthetic",
            name="Synthetic",
            x=0,
            y=0,
            width=1280,
            height=720,
            primary=True,
        )
        frame = source.grab()
        assert frame is not None
        assert frame.shape == (720, 1280, 4)
        assert frame.flags.c_contiguous
        assert frame.dtype == np.uint8
    finally:
        source.close()
