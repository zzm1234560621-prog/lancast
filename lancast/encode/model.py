"""Value types and the encoder protocol shared by H.264 and JPEG encoders."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import numpy as np

__all__ = ["H264Config", "JpegConfig", "EncodedFrame", "Encoder", "fit_size"]


@dataclass(frozen=True)
class H264Config:
    """Everything the browser needs to configure a WebCodecs H.264 decoder."""

    codec_string: str  # e.g. "avc1.640028"
    description: bytes  # AVCDecoderConfigurationRecord ("avcC")
    width: int
    height: int


@dataclass(frozen=True)
class JpegConfig:
    width: int
    height: int


@dataclass(frozen=True)
class EncodedFrame:
    """One encoded video access unit (H.264 AVCC) or one JPEG file."""

    data: bytes
    keyframe: bool
    pts_us: int
    config: "H264Config | JpegConfig"


@runtime_checkable
class Encoder(Protocol):
    codec: str
    name: str
    out_width: int
    out_height: int

    def encode(
        self,
        bgra: np.ndarray,
        pts_us: int,
        force_keyframe: bool = False,
    ) -> "EncodedFrame | None":
        """Encode one BGRA frame, returning its access unit in the same call."""
        ...

    def close(self) -> None: ...


def fit_size(src_w: int, src_h: int, max_w: int, max_h: int) -> tuple[int, int]:
    """Fit ``src_w x src_h`` inside ``max_w x max_h`` keeping aspect ratio.

    Never upscales, both returned dimensions are even (rounded down) and at
    least 2.  Example: ``fit_size(2880, 1800, 1920, 1080) == (1728, 1080)``.
    """
    if src_w <= 0 or src_h <= 0:
        raise ValueError("source dimensions must be positive")
    if max_w <= 0 or max_h <= 0:
        raise ValueError("maximum dimensions must be positive")

    scale = min(1.0, max_w / float(src_w), max_h / float(src_h))
    width = int(src_w * scale)
    height = int(src_h * scale)
    width -= width % 2
    height -= height % 2
    return max(2, width), max(2, height)
