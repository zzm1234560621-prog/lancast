"""lancast.encode — low-latency H.264 and JPEG encoders.

Public surface (see docs/ARCHITECTURE.md):

* :class:`H264Config`, :class:`JpegConfig`, :class:`EncodedFrame`
* :class:`Encoder` protocol
* :func:`fit_size`
* :func:`probe_h264_encoders` (cached)
* :func:`create_encoder`

H.264 payloads are one access unit in AVCC form with SPS/PPS/AUD/SEI
removed; parameter sets travel in ``config.description`` (avcC).
"""

from __future__ import annotations

import logging
import os

from .h264 import H264Encoder, probe_h264_encoders
from .jpeg import JpegEncoder
from .model import EncodedFrame, Encoder, H264Config, JpegConfig, fit_size

__all__ = [
    "H264Config",
    "JpegConfig",
    "EncodedFrame",
    "Encoder",
    "fit_size",
    "probe_h264_encoders",
    "preferred_h264_backends",
    "create_encoder",
]

log = logging.getLogger("lancast.encode")


def preferred_h264_backends() -> list[str]:
    """Working backends in the order ``backend="auto"`` tries them: lowest latency first.

    Through PyAV, Intel QSV needs ~10 ms per 1080p frame (upload + synchronous
    encode) while libx264 with ``tune=zerolatency`` needs ~4 ms on a modern
    8+ core CPU, so libx264 wins there. On small CPUs hardware encoders go first.
    Pass ``--encoder h264_qsv`` (or another backend) to trade latency for CPU.
    """
    working = probe_h264_encoders()
    if "libx264" in working and (os.cpu_count() or 1) >= 8:
        return ["libx264"] + [name for name in working if name != "libx264"]
    return list(working)


def create_encoder(
    codec: str,
    src_width: int,
    src_height: int,
    out_width: int,
    out_height: int,
    fps: int,
    bitrate_kbps: int,
    jpeg_quality: int = 70,
    backend: str = "auto",
):
    """Create a JPEG or H.264 encoder.

    For H.264 with ``backend="auto"`` the first working probe result is used.
    An explicitly requested backend is tried first; if it cannot be opened,
    the remaining probe results are tried in order and a warning is logged.
    """
    codec = codec.lower()
    if codec == "jpeg":
        return JpegEncoder(
            src_width, src_height, out_width, out_height, quality=jpeg_quality
        )
    if codec != "h264":
        raise ValueError("unsupported codec %r (expected 'h264' or 'jpeg')" % codec)

    working = probe_h264_encoders()
    if backend and backend != "auto":
        requested = backend
        candidates = [requested] + [name for name in working if name != requested]
    else:
        requested = None
        candidates = preferred_h264_backends()

    if not candidates:
        raise RuntimeError("no working H.264 encoder found on this machine")

    last_exc: Exception | None = None
    for candidate in candidates:
        try:
            return H264Encoder(
                candidate,
                src_width,
                src_height,
                out_width,
                out_height,
                fps,
                bitrate_kbps,
            )
        except Exception as exc:  # noqa: BLE001 - fall back to the next backend
            last_exc = exc
            if requested is not None and candidate == requested:
                log.warning(
                    "H.264 backend %s failed to open (%s); falling back",
                    candidate,
                    exc,
                )
            else:
                log.warning(
                    "H.264 backend %s failed to open (%s); trying next", candidate, exc
                )
    raise RuntimeError(
        "could not create an H.264 encoder (last error: %s)" % last_exc
    ) from last_exc
