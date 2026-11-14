"""End-to-end tests for the H.264 encoders, JPEG fallback and fit_size."""

from __future__ import annotations

import cv2
import numpy as np
import av
import pytest

from lancast.encode import (
    EncodedFrame,
    H264Config,
    JpegConfig,
    create_encoder,
    fit_size,
    probe_h264_encoders,
)
from lancast.encode.h264 import BACKENDS
from lancast.encode.h264util import (
    avcc_to_annexb,
    codec_string,
    nal_type,
    parse_avcc_record,
)

WORKING = probe_h264_encoders()
SRC_W, SRC_H = 1920, 1200
OUT_W, OUT_H = fit_size(SRC_W, SRC_H, 1920, 1080)


def _base_frame() -> np.ndarray:
    frame = np.empty((SRC_H, SRC_W, 4), dtype=np.uint8)
    x = np.linspace(10, 90, SRC_W, dtype=np.uint8)[None, :]
    y = np.linspace(20, 120, SRC_H, dtype=np.uint8)[:, None]
    frame[..., 0] = y
    frame[..., 1] = x
    frame[..., 2] = 140
    frame[..., 3] = 255
    for row in range(0, SRC_H, 48):
        cv2.line(frame, (0, row), (SRC_W - 1, row), (55, 55, 55, 255), 1)
    return frame


_BASE = _base_frame()


def _frame(index: int) -> np.ndarray:
    frame = _BASE.copy()
    x = 40 + (index * 11) % (SRC_W - 500)
    y = 60 + (index * 7) % (SRC_H - 300)
    cv2.rectangle(frame, (x, y), (x + 420, y + 240), (0, 0, 230, 255), -1)
    cv2.rectangle(frame, (x + 400, y + 180), (x + 620, y + 420), (230, 120, 0, 255), 3)
    cv2.putText(
        frame,
        "LanCast frame %03d" % index,
        (60, SRC_H - 80),
        cv2.FONT_HERSHEY_SIMPLEX,
        2.2,
        (255, 255, 255, 255),
        5,
        cv2.LINE_AA,
    )
    cv2.line(
        frame,
        (0, SRC_H - 1 - index * 3),
        (SRC_W - 1, index * 3),
        (255, 255, 255, 255),
        1,
    )
    return frame


def _avcc_nal_types(data: bytes) -> list[int]:
    types: list[int] = []
    pos = 0
    while pos + 4 <= len(data):
        length = int.from_bytes(data[pos : pos + 4], "big")
        pos += 4
        assert length > 0
        nal = data[pos : pos + length]
        assert len(nal) == length
        types.append(nal_type(nal))
        pos += length
    assert pos == len(data)
    return types


def _decode_records(
    records: list[tuple[bytes, bool]], config: H264Config
) -> list[av.VideoFrame]:
    sps, pps = parse_avcc_record(config.description)
    prefix = b"\x00\x00\x00\x01" + sps + b"\x00\x00\x00\x01" + pps
    decoder = av.CodecContext.create("h264", "r")
    frames: list[av.VideoFrame] = []
    for avcc, keyframe in records:
        data = (prefix if keyframe else b"") + avcc_to_annexb(avcc)
        frames.extend(decoder.decode(av.Packet(data)))
    try:
        frames.extend(decoder.decode(None))
    except (ValueError, TypeError, av.error.FFmpegError):
        pass
    return frames


def test_probe_lists_libx264_and_is_cached():
    assert "libx264" in WORKING
    assert all(name in BACKENDS for name in WORKING)
    assert probe_h264_encoders() == WORKING
    # A second call must return a copy, not the cached list object.
    assert probe_h264_encoders() is not probe_h264_encoders()


@pytest.mark.parametrize("backend", WORKING)
def test_h264_encoder_120_frames(backend):
    assert (OUT_W, OUT_H) == (1728, 1080)
    encoder = create_encoder(
        "h264",
        SRC_W,
        SRC_H,
        OUT_W,
        OUT_H,
        fps=60,
        bitrate_kbps=10000,
        backend=backend,
    )
    assert encoder.name == backend
    assert encoder.codec == "h264"
    assert (encoder.out_width, encoder.out_height) == (OUT_W, OUT_H)

    records: list[tuple[bytes, bool]] = []
    config = None
    try:
        for index in range(120):
            pts_us = index * 16666
            packet = encoder.encode(
                _frame(index), pts_us, force_keyframe=(index == 49)
            )
            assert packet is not None, "frame %d was dropped" % index
            assert isinstance(packet, EncodedFrame)
            assert packet.pts_us == pts_us
            assert isinstance(packet.config, H264Config)
            assert packet.config.width == OUT_W
            assert packet.config.height == OUT_H
            assert packet.config.description
            assert packet.config.codec_string.startswith("avc1.")
            if config is None:
                config = packet.config
            else:
                # Frozen dataclass equality: unchanged configuration compares
                # equal to the previous frame's config.
                assert packet.config == config

            types = _avcc_nal_types(packet.data)
            assert types, "empty access unit"
            assert not ({6, 7, 8, 9} & set(types)), types
            if packet.keyframe:
                assert 5 in types
            if index == 0:
                assert packet.keyframe, "first frame must be an IDR"
            if index == 49:
                assert packet.keyframe, "forced keyframe must be an IDR"
            records.append((packet.data, packet.keyframe))
    finally:
        encoder.close()

    assert config is not None
    sps, pps = parse_avcc_record(config.description)
    assert config.codec_string == codec_string(sps)
    decoded = _decode_records(records, config)
    assert len(decoded) == 120
    assert all(
        (frame.width, frame.height) == (OUT_W, OUT_H) for frame in decoded
    )


def test_create_encoder_auto_uses_preferred_backend():
    from lancast.encode import preferred_h264_backends

    preferred = preferred_h264_backends()
    assert sorted(preferred) == sorted(WORKING)
    encoder = create_encoder(
        "h264", SRC_W, SRC_H, OUT_W, OUT_H, 60, 10000, backend="auto"
    )
    try:
        assert encoder.name == preferred[0]
    finally:
        encoder.close()


def test_create_encoder_falls_back_from_unknown_backend():
    encoder = create_encoder(
        "h264",
        SRC_W,
        SRC_H,
        OUT_W,
        OUT_H,
        60,
        10000,
        backend="not-a-real-encoder",
    )
    try:
        assert encoder.name in WORKING
    finally:
        encoder.close()


def test_jpeg_encoder():
    encoder = create_encoder(
        "jpeg", 1280, 720, 640, 360, 30, 0, jpeg_quality=70, backend="auto"
    )
    try:
        assert encoder.codec == "jpeg"
        assert encoder.name == "jpeg"
        first_config = None
        for index in range(5):
            frame = np.zeros((720, 1280, 4), dtype=np.uint8)
            frame[..., 0] = 40
            frame[..., 1] = 120
            frame[..., 2] = 210
            frame[..., 3] = 255
            cv2.rectangle(
                frame,
                (100 + index * 20, 100),
                (500 + index * 20, 500),
                (0, 0, 255, 255),
                -1,
            )
            packet = encoder.encode(frame, index * 33333)
            assert packet is not None
            assert packet.keyframe is True
            assert packet.pts_us == index * 33333
            assert isinstance(packet.config, JpegConfig)
            assert packet.config == JpegConfig(640, 360)
            if first_config is None:
                first_config = packet.config
            assert packet.config == first_config
            assert packet.data[:2] == b"\xff\xd8"
            decoded = cv2.imdecode(
                np.frombuffer(packet.data, dtype=np.uint8), cv2.IMREAD_COLOR
            )
            assert decoded is not None
            assert decoded.shape == (360, 640, 3)
    finally:
        encoder.close()


def test_fit_size_aspect_and_no_upscale():
    assert fit_size(2880, 1800, 1920, 1080) == (1728, 1080)
    assert fit_size(1920, 1200, 1920, 1080) == (1728, 1080)
    assert fit_size(640, 480, 1920, 1080) == (640, 480)
    width, height = fit_size(1366, 768, 800, 600)
    assert width <= 800 and height <= 600
    assert width % 2 == 0 and height % 2 == 0
    assert fit_size(1, 1, 100, 100) == (2, 2)
    for src_w, src_h, max_w, max_h in [
        (2880, 1800, 1920, 1080),
        (1366, 768, 800, 600),
        (1919, 1079, 1920, 1080),
    ]:
        width, height = fit_size(src_w, src_h, max_w, max_h)
        assert width <= max_w and height <= max_h
        assert width % 2 == 0 and height % 2 == 0
        assert width >= 2 and height >= 2

    with pytest.raises(ValueError):
        fit_size(0, 100, 100, 100)
