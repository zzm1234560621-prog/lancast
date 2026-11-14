"""Animated synthetic capture source — no OS or GPU dependency.

Used by tests, by ``--capture synthetic`` style development runs and as a
last resort when diagnosing encoder latency.  The pattern is a moving
vertical bar over a gradient, with the frame number drawn on top.
"""

from __future__ import annotations

import time

import cv2
import numpy as np

from .base import DisplayInfo

_SYNTHETIC_DISPLAY = DisplayInfo(
    index=0,
    id="synthetic",
    name="Synthetic",
    x=0,
    y=0,
    width=1280,
    height=720,
    primary=True,
)


class SyntheticCapture:
    """Generate a moving BGRA test pattern at a fixed maximum frame rate."""

    backend = "synthetic"

    def __init__(self, width: int = 1280, height: int = 720, fps: int = 60) -> None:
        if width <= 0 or height <= 0:
            raise ValueError("width and height must be positive")
        if fps <= 0:
            raise ValueError("fps must be positive")

        self.width = int(width)
        self.height = int(height)
        self.fps = int(fps)
        self.display = DisplayInfo(
            index=_SYNTHETIC_DISPLAY.index,
            id=_SYNTHETIC_DISPLAY.id,
            name=_SYNTHETIC_DISPLAY.name,
            x=_SYNTHETIC_DISPLAY.x,
            y=_SYNTHETIC_DISPLAY.y,
            width=self.width,
            height=self.height,
            primary=True,
        )
        self._interval = 1.0 / float(self.fps)
        self._last_emit: float | None = None
        self._frame_no = 0
        self._closed = False
        self._bar_width = max(4, self.width // 16)
        self._font_scale = max(0.5, min(2.0, self.width / 900.0))
        self._thickness = 2 if self.width >= 640 else 1

        # One-time gradient background: warm at the bottom, cool at the top,
        # with a slight horizontal component.
        ys = np.linspace(0.0, 1.0, self.height, dtype=np.float32)[:, None]
        xs = np.linspace(0.0, 1.0, self.width, dtype=np.float32)[None, :]
        background = np.empty((self.height, self.width, 4), dtype=np.uint8)
        background[..., 0] = (28.0 + 92.0 * ys + 12.0 * xs).clip(0, 255).astype(
            np.uint8
        )
        background[..., 1] = (20.0 + 60.0 * ys + 24.0 * xs).clip(0, 255).astype(
            np.uint8
        )
        background[..., 2] = (18.0 + 90.0 * ys + 30.0 * (1.0 - xs)).clip(
            0, 255
        ).astype(np.uint8)
        background[..., 3] = 255
        self._background = background

    def grab(self) -> np.ndarray | None:
        if self._closed:
            return None

        now = time.perf_counter()
        if self._last_emit is not None and (now - self._last_emit) < self._interval:
            return None
        self._last_emit = now

        frame_no = self._frame_no
        self._frame_no += 1

        frame = self._background.copy()

        travel = self.width + self._bar_width
        bar_x = int((frame_no * max(2, self.width // 160)) % travel) - self._bar_width
        left = max(0, bar_x)
        right = min(self.width, bar_x + self._bar_width)
        if right > left:
            frame[:, left:right, 0] = 0
            frame[:, left:right, 1] = 200
            frame[:, left:right, 2] = 255

        text_y = self.height - max(18, int(26 * self._font_scale))
        cv2.putText(
            frame,
            f"frame {frame_no}  {self.fps} fps",
            (max(8, self.width // 60), text_y),
            cv2.FONT_HERSHEY_SIMPLEX,
            self._font_scale,
            (255, 255, 255, 255),
            self._thickness,
            cv2.LINE_AA,
        )
        return np.ascontiguousarray(frame)

    def close(self) -> None:
        self._closed = True


def open_synthetic(
    width: int = 1280, height: int = 720, fps: int = 60
) -> SyntheticCapture:
    """Create an animated test-pattern source."""

    return SyntheticCapture(width=width, height=height, fps=fps)
