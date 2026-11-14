"""PyAV-backed low-latency H.264 encoder and backend probing."""

from __future__ import annotations

import logging
import threading
from fractions import Fraction

import av
import cv2
import numpy as np

from .h264util import (
    annexb_to_frame,
    avcc_to_annexb,
    build_avcc_record,
    codec_string,
    parse_avcc_record,
    split_annexb,
)
from .model import EncodedFrame, H264Config

__all__ = ["BACKENDS", "probe_h264_encoders", "H264Encoder"]

log = logging.getLogger("lancast.encode")

# Best first.  h264_d3d12va is a long shot (it needs D3D12 hardware frames) and
# deliberately comes after the software fallback so it can never become the
# automatic default.
BACKENDS: tuple[str, ...] = (
    "h264_qsv",
    "h264_nvenc",
    "h264_amf",
    "h264_mf",
    "libx264",
    "h264_d3d12va",
)

_PROBE_LOCK = threading.Lock()
_PROBE_CACHE: list[str] | None = None

# PyAV 18 exposes PictureType on av.video.frame; keep a fallback for builds
# where the enum lives somewhere else (the numeric value is AVPictureType.I).
try:
    _PICTURE_TYPE_I = av.video.frame.PictureType.I
except AttributeError:  # pragma: no cover - compatibility shim
    _PICTURE_TYPE_I = 1

_PROBE_W = 640
_PROBE_H = 360
_PROBE_FPS = 30
_PROBE_FRAMES = 10
_PROBE_FORCE_AT = 5


def probe_h264_encoders() -> list[str]:
    """Return the H.264 backends that actually work on this machine, best first.

    The result is computed once and cached at module level.  A backend is only
    accepted when it opens, returns a packet for every single input frame
    (zero buffering), emits an IDR for the first and for a forced keyframe,
    and its output decodes with PyAV's H.264 decoder.
    """
    global _PROBE_CACHE
    if _PROBE_CACHE is not None:
        return list(_PROBE_CACHE)
    with _PROBE_LOCK:
        if _PROBE_CACHE is None:
            working: list[str] = []
            for backend in BACKENDS:
                if backend not in av.codecs_available:
                    log.debug("h264 backend %s not available in this PyAV build", backend)
                    continue
                try:
                    _probe_backend(backend)
                except Exception as exc:  # noqa: BLE001 - probe must never escape
                    log.debug("h264 backend %s not usable: %s", backend, exc)
                    continue
                working.append(backend)
                log.debug("h264 backend %s is working", backend)
            _PROBE_CACHE = working
            log.info("working H.264 encoders: %s", ", ".join(working) or "(none)")
    return list(_PROBE_CACHE)


def _choose_pix_fmt(codec: av.Codec) -> str:
    names = {fmt.name for fmt in codec.video_formats}
    if "yuv420p" in names:
        return "yuv420p"
    if "nv12" in names:
        return "nv12"
    raise RuntimeError(
        "codec %s has no software 4:2:0 pixel format (has: %s)"
        % (codec.name, ", ".join(sorted(names)))
    )


def _options_for(backend: str, bitrate_kbps: int, fps: int) -> dict[str, str]:
    bps = max(1, int(bitrate_kbps)) * 1000
    if backend == "h264_qsv":
        return {
            "preset": "veryfast",
            "async_depth": "1",
            "forced_idr": "1",
            "look_ahead": "0",
            "low_power": "0",
            "low_delay_brc": "1",
            "pic_timing_sei": "0",
            "maxrate": str(bps),
            "minrate": str(bps),
            "bufsize": str(max(bps // 2, bps // fps * 2)),
        }
    if backend == "libx264":
        return {
            "preset": "superfast",
            "tune": "zerolatency",
            "profile": "high",
            "forced-idr": "1",
            "rc-lookahead": "0",
            "maxrate": str(int(bps * 1.5)),
            "bufsize": str(bps),
        }
    if backend == "h264_mf":
        return {
            "hw_encoding": "1",
            "scenario": "displayremoting",
        }
    if backend == "h264_nvenc":
        return {
            "preset": "p1",
            "tune": "ull",
            "rc": "cbr",
            "zerolatency": "1",
            "forced-idr": "1",
            "maxrate": str(bps),
            "bufsize": str(max(bps // 2, 1)),
        }
    if backend == "h264_amf":
        return {
            "usage": "ultralowlatency",
            "quality": "speed",
            "rc": "cbr",
            "forced_idr": "1",
            "maxrate": str(bps),
            "bufsize": str(max(bps // 2, 1)),
        }
    return {}


# Options to remove, in order, if opening with the full set fails.  The last
# entries are the low-latency essentials; probes reject backends that lose
# them anyway.
_DROP_ORDER: dict[str, tuple[str, ...]] = {
    "h264_qsv": (
        "pic_timing_sei",
        "low_delay_brc",
        "look_ahead",
        "low_power",
        "minrate",
        "maxrate",
        "bufsize",
        "preset",
        "forced_idr",
    ),
    "libx264": (
        "maxrate",
        "bufsize",
        "rc-lookahead",
        "profile",
        "preset",
        "tune",
        "forced-idr",
    ),
    "h264_mf": ("scenario", "hw_encoding"),
    "h264_nvenc": (
        "maxrate",
        "bufsize",
        "rc",
        "zerolatency",
        "tune",
        "preset",
        "forced-idr",
    ),
    "h264_amf": (
        "maxrate",
        "bufsize",
        "rc",
        "quality",
        "usage",
        "forced_idr",
    ),
}


def _open_context(
    backend: str,
    width: int,
    height: int,
    fps: int,
    bitrate_kbps: int,
):
    """Open a PyAV encoder context, dropping options the backend rejects."""
    if backend not in av.codecs_available:
        raise RuntimeError("codec %s is not available" % backend)

    options = _options_for(backend, bitrate_kbps, fps)
    drop_order = list(_DROP_ORDER.get(backend, tuple(options)))
    attempts: list[tuple[dict[str, str], str | None]] = [(dict(options), None)]
    trimmed = dict(options)
    for key in drop_order:
        if key not in trimmed:
            continue
        trimmed.pop(key)
        attempts.append((dict(trimmed), key))

    last_exc: Exception | None = None
    for opts, dropped in attempts:
        ctx = None
        try:
            ctx = av.CodecContext.create(backend, "w")
            ctx.width = int(width)
            ctx.height = int(height)
            ctx.time_base = Fraction(1, fps)
            ctx.framerate = Fraction(fps, 1)
            ctx.bit_rate = max(1, int(bitrate_kbps)) * 1000
            ctx.max_b_frames = 0
            ctx.gop_size = max(1, fps * 4)
            ctx.pix_fmt = _choose_pix_fmt(ctx.codec)
            ctx.options = dict(opts)
            ctx.open()
            pix_fmt = ctx.format.name
            if pix_fmt not in ("yuv420p", "nv12"):
                raise RuntimeError(
                    "codec only offers non-system pixel format %r" % pix_fmt
                )
            if dropped is not None:
                log.debug("%s: dropped option %r after open failure", backend, dropped)
            return ctx, pix_fmt
        except Exception as exc:  # noqa: BLE001 - try the next reduced set
            last_exc = exc
            log.debug(
                "%s: open failed with options %s: %s",
                backend,
                opts,
                exc,
            )
            ctx = None
    raise RuntimeError(
        "could not open %s with any option set: %s" % (backend, last_exc)
    ) from last_exc


def _i420_to_nv12(i420: np.ndarray) -> np.ndarray:
    """Interleave I420's U/V planes into NV12 without an extra colour pass."""
    height = i420.shape[0] * 2 // 3
    width = i420.shape[1]
    uv = i420[height:].reshape(2, height // 2, width // 2)
    nv12 = np.empty((height + height // 2, width), dtype=np.uint8)
    nv12[:height] = i420[:height]
    interleaved = nv12[height:].reshape(height // 2, width // 2, 2)
    interleaved[..., 0] = uv[0]
    interleaved[..., 1] = uv[1]
    return nv12


def _bgra_to_i420(bgra: np.ndarray, out_w: int, out_h: int) -> np.ndarray:
    """BGRA (src size) -> I420 (out size), shape (out_h * 3 // 2, out_w).

    Scale first, then convert. (Converting at full resolution and shrinking the
    planes is ~20 % faster, but OpenCV's I420 conversion point-samples chroma,
    so coloured text would alias before the area filter could average it.)
    """
    src_h, src_w = bgra.shape[:2]
    if (src_w, src_h) != (out_w, out_h):
        shrinking = out_w * out_h < src_w * src_h
        bgra = cv2.resize(
            bgra, (out_w, out_h), interpolation=cv2.INTER_AREA if shrinking else cv2.INTER_LINEAR
        )
    return cv2.cvtColor(bgra, cv2.COLOR_BGRA2YUV_I420)


def _bgra_to_video_frame(
    bgra: np.ndarray,
    out_w: int,
    out_h: int,
    pix_fmt: str,
) -> av.VideoFrame:
    i420 = _bgra_to_i420(bgra, out_w, out_h)
    if pix_fmt == "nv12":
        array = _i420_to_nv12(i420)
    else:
        array = i420
    return av.VideoFrame.from_ndarray(array, format=pix_fmt)


def _avcc_from_extradata(
    extradata: bytes | None,
) -> tuple[bytes | None, bytes | None, bytes | None]:
    """Return ``(description, sps, pps)`` from a codec's extradata."""
    if not extradata:
        return None, None, None
    data = bytes(extradata)
    if len(data) >= 7 and data[0] == 1 and data[4] == 0xFF:
        try:
            sps, pps = parse_avcc_record(data)
            return data, sps, pps
        except Exception:  # noqa: BLE001 - fall through to Annex B parsing
            pass
    sps = pps = None
    for nal in split_annexb(data):
        ntype = nal[0] & 0x1F
        if ntype == 7 and sps is None:
            sps = nal
        elif ntype == 8 and pps is None:
            pps = nal
    if sps is None or pps is None:
        return None, sps, pps
    try:
        return build_avcc_record(sps, pps), sps, pps
    except Exception:
        return None, sps, pps


def _decode_records(
    records: list[tuple[bytes, bool]],
    description: bytes,
    width: int,
    height: int,
) -> int:
    """Decode AVCC records (prepending parameter sets at keyframes)."""
    sps, pps = parse_avcc_record(description)
    prefix = b"\x00\x00\x00\x01" + sps + b"\x00\x00\x00\x01" + pps
    decoder = av.CodecContext.create("h264", "r")
    count = 0
    for avcc, keyframe in records:
        if not avcc:
            continue
        data = (prefix if keyframe else b"") + avcc_to_annexb(avcc)
        for frame in decoder.decode(av.Packet(data)):
            count += 1
            if (frame.width, frame.height) != (width, height):
                raise RuntimeError(
                    "decoded frame is %dx%d, expected %dx%d"
                    % (frame.width, frame.height, width, height)
                )
    try:
        for frame in decoder.decode(None):  # flush delayed frames
            count += 1
            if (frame.width, frame.height) != (width, height):
                raise RuntimeError(
                    "decoded frame is %dx%d, expected %dx%d"
                    % (frame.width, frame.height, width, height)
                )
    except (ValueError, TypeError, av.error.FFmpegError):
        pass
    finally:
        decoder = None
    return count


def _probe_frame(width: int, height: int, index: int) -> np.ndarray:
    """Synthetic BGRA frame with motion and detail for the backend probe."""
    frame = np.empty((height, width, 4), dtype=np.uint8)
    # Vertical gradient background (BGR channels; alpha 255).
    column = np.linspace(20, 220, height, dtype=np.uint8)[:, None]
    frame[..., 0] = column
    frame[..., 1] = np.linspace(30, 200, width, dtype=np.uint8)[None, :]
    frame[..., 2] = 80
    frame[..., 3] = 255
    x = (index * 37) % max(1, width - 120)
    y = (index * 23) % max(1, height - 100)
    frame[y : y + 100, x : x + 120] = (0, 0, 230, 255)
    cv2.line(frame, (0, height - 1 - index * 4), (width - 1, index * 4), (255, 255, 255, 255), 1)
    for row in range(0, height, 32):
        cv2.line(frame, (0, row), (width, row), (40, 40, 40, 255), 1)
    return frame


def _probe_backend(backend: str) -> None:
    ctx = None
    try:
        ctx, pix_fmt = _open_context(backend, _PROBE_W, _PROBE_H, _PROBE_FPS, 2000)
        description, sps, pps = _avcc_from_extradata(ctx.extradata)
        records: list[tuple[bytes, bool]] = []

        for index in range(_PROBE_FRAMES):
            bgra = _probe_frame(_PROBE_W, _PROBE_H, index)
            frame = _bgra_to_video_frame(bgra, _PROBE_W, _PROBE_H, pix_fmt)
            frame.pts = index
            frame.time_base = Fraction(1, _PROBE_FPS)
            if index == 0 or index == _PROBE_FORCE_AT:
                frame.pict_type = _PICTURE_TYPE_I
            packets = ctx.encode(frame)
            if not packets:
                raise RuntimeError(
                    "no packet returned for frame %d (encoder buffers output)" % index
                )
            payload = b"".join(bytes(packet) for packet in packets)
            nals = split_annexb(payload)
            types = [nal[0] & 0x1F for nal in nals]
            if index == 0 and 5 not in types:
                raise RuntimeError("first encoded frame is not an IDR")
            if index == _PROBE_FORCE_AT and 5 not in types:
                raise RuntimeError("force_keyframe did not produce an IDR")
            avcc, is_idr, new_sps, new_pps = annexb_to_frame(payload)
            if new_sps is not None:
                sps = new_sps
            if new_pps is not None:
                pps = new_pps
            if sps is not None and pps is not None:
                description = build_avcc_record(sps, pps)
            records.append((avcc, is_idr))

        if description is None:
            raise RuntimeError("encoder produced no SPS/PPS")
        decoded = _decode_records(records, description, _PROBE_W, _PROBE_H)
        if decoded != _PROBE_FRAMES:
            raise RuntimeError(
                "decoded %d frames, expected %d" % (decoded, _PROBE_FRAMES)
            )
    finally:
        ctx = None


class H264Encoder:
    """Low-latency H.264 encoder writing one AVCC access unit per input frame."""

    codec = "h264"

    def __init__(
        self,
        backend: str,
        src_width: int,
        src_height: int,
        out_width: int,
        out_height: int,
        fps: int,
        bitrate_kbps: int,
    ):
        if fps <= 0:
            raise ValueError("fps must be positive")
        self.name = backend
        self.src_width = int(src_width)
        self.src_height = int(src_height)
        self.out_width = int(out_width)
        self.out_height = int(out_height)
        self.fps = int(fps)
        self.bitrate_kbps = int(bitrate_kbps)
        self._ctx, self._pix_fmt = _open_context(
            backend, self.out_width, self.out_height, self.fps, self.bitrate_kbps
        )
        self._pts = 0
        self._config: H264Config | None = None
        self._sps: bytes | None = None
        self._pps: bytes | None = None
        description, sps, pps = _avcc_from_extradata(self._ctx.extradata)
        if description is not None and sps is not None and pps is not None:
            self._sps = sps
            self._pps = pps
            self._config = H264Config(
                codec_string(sps),
                description,
                self.out_width,
                self.out_height,
            )

    def _update_config(self, sps: bytes | None, pps: bytes | None) -> H264Config:
        if sps is not None:
            self._sps = sps
        if pps is not None:
            self._pps = pps
        if self._sps is None or self._pps is None:
            raise RuntimeError("H.264 frame arrived before SPS/PPS were available")
        description = build_avcc_record(self._sps, self._pps)
        current = self._config
        if current is not None and current.description == description:
            return current
        self._config = H264Config(
            codec_string(self._sps),
            description,
            self.out_width,
            self.out_height,
        )
        return self._config

    def _prepare_frame(
        self, bgra: np.ndarray, force_keyframe: bool = False
    ) -> av.VideoFrame:
        """Scale/convert one BGRA frame and stamp it with the next PTS.

        Kept separate from :meth:`_encode_packets` so benchmarks can time the
        conversion and the encoder independently while using the same code.
        """
        if self._ctx is None:
            raise RuntimeError("encoder is closed")
        if (
            not isinstance(bgra, np.ndarray)
            or bgra.dtype != np.uint8
            or bgra.ndim != 3
            or bgra.shape[2] != 4
        ):
            raise ValueError("bgra must be an (H, W, 4) uint8 numpy array")

        frame = _bgra_to_video_frame(
            bgra, self.out_width, self.out_height, self._pix_fmt
        )
        frame.pts = self._pts
        frame.time_base = Fraction(1, self.fps)
        if force_keyframe or self._pts == 0:
            frame.pict_type = _PICTURE_TYPE_I
        self._pts += 1
        return frame

    def _encode_packets(
        self,
        frame: av.VideoFrame,
        pts_us: int,
        force_keyframe: bool = False,
    ) -> EncodedFrame | None:
        """Run the prepared frame through the codec and build the AVCC unit."""
        if self._ctx is None:
            raise RuntimeError("encoder is closed")
        pts = frame.pts
        packets = self._ctx.encode(frame)
        if not packets:
            log.debug("%s: encoder returned no packet for pts %d", self.name, pts)
            return None

        chunks: list[bytes] = []
        saw_idr = False
        new_sps: bytes | None = None
        new_pps: bytes | None = None
        for packet in packets:
            avcc, is_idr, sps, pps = annexb_to_frame(bytes(packet))
            if sps is not None:
                new_sps = sps
            if pps is not None:
                new_pps = pps
            if avcc:
                chunks.append(avcc)
            if is_idr:
                saw_idr = True

        config = self._config
        if new_sps is not None or new_pps is not None or config is None:
            config = self._update_config(new_sps, new_pps)

        data = b"".join(chunks)
        if not data:
            log.debug("%s: encoder packet contained no slice data", self.name)
            return None
        if force_keyframe and not saw_idr:
            log.warning(
                "%s: force_keyframe did not produce an IDR (pts %d)", self.name, pts
            )
        return EncodedFrame(
            data=data,
            keyframe=saw_idr,
            pts_us=int(pts_us),
            config=config,
        )

    def encode(
        self,
        bgra: np.ndarray,
        pts_us: int,
        force_keyframe: bool = False,
    ) -> EncodedFrame | None:
        frame = self._prepare_frame(bgra, force_keyframe)
        return self._encode_packets(frame, pts_us, force_keyframe)

    def close(self) -> None:
        self._ctx = None
