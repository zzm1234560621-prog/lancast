"""JPEG fallback encoder used by browsers without WebCodecs."""

from __future__ import annotations

import logging

import cv2
import numpy as np

from .model import EncodedFrame, JpegConfig

__all__ = ["JpegEncoder"]

log = logging.getLogger("lancast.encode")


class JpegEncoder:
    """Scale a BGRA frame and encode it as a complete baseline JPEG file."""

    codec = "jpeg"
    name = "jpeg"

    def __init__(
        self,
        src_width: int,
        src_height: int,
        out_width: int,
        out_height: int,
        quality: int = 70,
    ):
        self.src_width = int(src_width)
        self.src_height = int(src_height)
        self.out_width = int(out_width)
        self.out_height = int(out_height)
        self.quality = max(1, min(100, int(quality)))
        self._config = JpegConfig(self.out_width, self.out_height)
        self._params = [int(cv2.IMWRITE_JPEG_QUALITY), self.quality]

    def encode(
        self,
        bgra: np.ndarray,
        pts_us: int,
        force_keyframe: bool = False,
    ) -> EncodedFrame | None:
        if (
            not isinstance(bgra, np.ndarray)
            or bgra.dtype != np.uint8
            or bgra.ndim != 3
            or bgra.shape[2] != 4
        ):
            raise ValueError("bgra must be an (H, W, 4) uint8 numpy array")

        src_h, src_w = bgra.shape[:2]
        if (src_w, src_h) != (self.out_width, self.out_height):
            shrinking = self.out_width * self.out_height < src_w * src_h
            resized = cv2.resize(
                bgra,
                (self.out_width, self.out_height),
                interpolation=cv2.INTER_AREA if shrinking else cv2.INTER_LINEAR,
            )
        else:
            resized = bgra
        bgr = cv2.cvtColor(resized, cv2.COLOR_BGRA2BGR)
        ok, buffer = cv2.imencode(".jpg", bgr, self._params)
        if not ok:
            log.warning("jpeg: cv2.imencode failed")
            return None
        return EncodedFrame(
            data=buffer.tobytes(),
            keyframe=True,
            pts_us=int(pts_us),
            config=self._config,
        )

    def close(self) -> None:
        return None
