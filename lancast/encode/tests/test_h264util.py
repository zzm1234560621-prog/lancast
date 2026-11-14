"""Unit tests for the pure H.264 bitstream helpers."""

from __future__ import annotations

import fractions

import av
import cv2
import numpy as np
import pytest

from lancast.encode.h264util import (
    annexb_to_frame,
    avcc_to_annexb,
    build_avcc_record,
    codec_string,
    nal_type,
    parse_avcc_record,
    split_annexb,
    to_avcc,
)


def _first_access_unit(profile: str) -> bytes:
    """Ask libx264 for one frame and return its raw Annex B access unit."""
    ctx = av.CodecContext.create("libx264", "w")
    ctx.width = 320
    ctx.height = 240
    ctx.time_base = fractions.Fraction(1, 30)
    ctx.framerate = fractions.Fraction(30, 1)
    ctx.bit_rate = 1_000_000
    ctx.max_b_frames = 0
    ctx.gop_size = 120
    ctx.pix_fmt = "yuv420p"
    ctx.options = {
        "preset": "superfast",
        "tune": "zerolatency",
        "profile": profile,
        "forced-idr": "1",
    }
    ctx.open()
    bgra = np.zeros((240, 320, 4), dtype=np.uint8)
    bgra[..., 0] = 30
    bgra[..., 1] = 100
    bgra[..., 2] = 200
    bgra[..., 3] = 255
    cv2.rectangle(bgra, (20, 20), (200, 180), (0, 0, 255, 255), -1)
    i420 = cv2.cvtColor(bgra, cv2.COLOR_BGRA2YUV_I420)
    frame = av.VideoFrame.from_ndarray(i420, format="yuv420p")
    frame.pts = 0
    frame.time_base = fractions.Fraction(1, 30)
    frame.pict_type = av.video.frame.PictureType.I
    packets = ctx.encode(frame)
    assert packets
    return b"".join(bytes(packet) for packet in packets)


def _sps_pps(profile: str) -> tuple[bytes, bytes]:
    nals = split_annexb(_first_access_unit(profile))
    sps = next(nal for nal in nals if nal_type(nal) == 7)
    pps = next(nal for nal in nals if nal_type(nal) == 8)
    return sps, pps


def test_split_annexb_mixed_start_codes_and_trailing_zeros():
    sps = bytes((0x67, 0x42, 0x00, 0x1E, 0xAB, 0xCD))
    pps = bytes((0x68, 0xCE, 0x3C, 0x80))
    aud = bytes((0x09, 0xF0))
    sei = bytes((0x06, 0x05, 0x01, 0x02, 0x03, 0x04, 0x05))
    idr = bytes((0x65, 0x88, 0x84, 0x21))
    non_idr = bytes((0x41, 0x9A, 0x21))

    data = (
        b"\x00\x00\x00\x01"
        + sps
        + b"\x00\x00\x01"
        + pps
        + b"\x00\x00\x00\x01"
        + aud
        + b"\x00\x00\x01"
        + sei
        + b"\x00\x00\x00\x01"
        + idr
        + b"\x00\x00\x01"
        + non_idr
        + b"\x00\x00\x00"  # trailing_zero_8bits
    )
    assert split_annexb(data) == [sps, pps, aud, sei, idr, non_idr]


def test_split_annexb_empty_and_no_start_code():
    assert split_annexb(b"") == []
    assert split_annexb(b"\x00\x00\x00\x00") == []
    assert split_annexb(b"\x67\x42\x00\x1e") == []


def test_nal_type():
    assert nal_type(b"\x67\x42") == 7
    assert nal_type(b"\x68\xce") == 8
    assert nal_type(b"\x65\x88") == 5
    assert nal_type(b"\x41\x9a") == 1
    with pytest.raises(ValueError):
        nal_type(b"")


def test_to_avcc_and_back():
    nals = [b"\x65\x88\x84", b"\x41\x9a\x21", b"\x06\x01\xff"]
    avcc = to_avcc(nals)
    assert avcc == (
        b"\x00\x00\x00\x03\x65\x88\x84"
        b"\x00\x00\x00\x03\x41\x9a\x21"
        b"\x00\x00\x00\x03\x06\x01\xff"
    )
    assert avcc_to_annexb(avcc) == (
        b"\x00\x00\x00\x01\x65\x88\x84"
        b"\x00\x00\x00\x01\x41\x9a\x21"
        b"\x00\x00\x00\x01\x06\x01\xff"
    )


def test_codec_string():
    assert codec_string(bytes((0x67, 0x64, 0x00, 0x28, 0xAA))) == "avc1.640028"
    assert codec_string(bytes((0x67, 0x42, 0xC0, 0x1E, 0xAA))) == "avc1.42c01e"


def test_build_avcc_record_baseline_sps():
    sps, pps = _sps_pps("baseline")
    assert sps[1] == 66
    record = build_avcc_record(sps, pps)
    assert record[0] == 1
    assert record[1:4] == sps[1:4]
    assert record[4] == 0xFF
    assert record[5] == 0xE1
    assert int.from_bytes(record[6:8], "big") == len(sps)
    assert record[8 : 8 + len(sps)] == sps
    pos = 8 + len(sps)
    assert record[pos] == 1
    assert int.from_bytes(record[pos + 1 : pos + 3], "big") == len(pps)
    assert record[pos + 3 :] == pps
    # 1 + 3 profile bytes + 0xFF + 0xE1 + u16 + sps + 1 + u16 + pps
    assert len(record) == 11 + len(sps) + len(pps)
    assert parse_avcc_record(record) == (sps, pps)


def test_build_avcc_record_high_sps_has_extension():
    sps, pps = _sps_pps("high")
    assert sps[1] == 100
    record = build_avcc_record(sps, pps)
    assert record[1:4] == sps[1:4]
    assert len(record) == 15 + len(sps) + len(pps)
    # 4:2:0, 8-bit: reserved bits + chroma 1, then bit depths 0, 0, ext count 0.
    assert record[-4:] == b"\xFD\xF8\xF8\x00"
    assert parse_avcc_record(record) == (sps, pps)
    assert codec_string(sps) == "avc1.640014"


def test_annexb_to_frame_strips_parameter_sets_and_metadata():
    sps = bytes((0x67, 0x42, 0x00, 0x1E, 0xAB))
    pps = bytes((0x68, 0xCE, 0x3C, 0x80))
    aud = bytes((0x09, 0xF0))
    sei = bytes((0x06, 0x05, 0x01))
    idr = bytes((0x65, 0x88, 0x84))
    non_idr = bytes((0x41, 0x9A, 0x21))
    data = (
        b"\x00\x00\x00\x01"
        + sps
        + b"\x00\x00\x01"
        + pps
        + b"\x00\x00\x01"
        + aud
        + b"\x00\x00\x01"
        + sei
        + b"\x00\x00\x01"
        + idr
        + b"\x00\x00\x01"
        + non_idr
    )
    avcc, is_idr, got_sps, got_pps = annexb_to_frame(data)
    assert avcc == to_avcc([idr, non_idr])
    assert is_idr is True
    assert got_sps == sps
    assert got_pps == pps

    avcc, is_idr, got_sps, got_pps = annexb_to_frame(b"\x00\x00\x01" + non_idr)
    assert avcc == to_avcc([non_idr])
    assert is_idr is False
    assert got_sps is None and got_pps is None
