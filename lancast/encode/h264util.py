"""Pure helpers for the H.264 bitstream used by LanCast.

The wire format is AVCC (ISO/IEC 14496-15): every NAL unit is prefixed with a
4-byte big-endian length.  Encoders built on FFmpeg/PyAV emit Annex B instead,
so the small conversion routines here sit on the hot path and must stay
allocation-light and dependency-free.
"""

from __future__ import annotations

__all__ = [
    "split_annexb",
    "nal_type",
    "to_avcc",
    "avcc_to_annexb",
    "parse_avcc_record",
    "build_avcc_record",
    "codec_string",
    "annexb_to_frame",
]

_HIGH_PROFILES = frozenset((100, 110, 122, 144))


def split_annexb(data: bytes) -> list[bytes]:
    """Split an Annex B byte stream into NAL units (start codes removed).

    Both 3-byte (``00 00 01``) and 4-byte (``00 00 00 01``) start codes are
    accepted, in any mix.  Leading bytes before the first start code and
    trailing zero bytes (``trailing_zero_8bits``) are ignored.  Empty NAL
    units are dropped.
    """
    if not data:
        return []
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("data must be bytes-like")
    buf = bytes(data)
    n = len(buf)
    starts: list[tuple[int, int]] = []
    pos = 0
    while pos + 2 < n:
        if buf[pos] == 0 and buf[pos + 1] == 0:
            if buf[pos + 2] == 1:
                starts.append((pos, 3))
                pos += 3
                continue
            if pos + 3 < n and buf[pos + 2] == 0 and buf[pos + 3] == 1:
                starts.append((pos, 4))
                pos += 4
                continue
        pos += 1

    if not starts:
        return []

    nals: list[bytes] = []
    for idx, (start, sc_len) in enumerate(starts):
        begin = start + sc_len
        end = starts[idx + 1][0] if idx + 1 < len(starts) else n
        nal = buf[begin:end].rstrip(b"\x00")
        if nal:
            nals.append(nal)
    return nals


def nal_type(nal: bytes) -> int:
    """Return the ``nal_unit_type`` field of a NAL unit."""
    if not nal:
        raise ValueError("empty NAL unit")
    return nal[0] & 0x1F


def to_avcc(nals: list[bytes]) -> bytes:
    """Convert a list of NAL units to AVCC form (u32 BE length + NAL)."""
    out = bytearray()
    for nal in nals:
        out += len(nal).to_bytes(4, "big")
        out += nal
    return bytes(out)


def avcc_to_annexb(data: bytes) -> bytes:
    """Convert AVCC bytes back to Annex B (4-byte start codes)."""
    out = bytearray()
    pos = 0
    n = len(data)
    while pos + 4 <= n:
        length = int.from_bytes(data[pos : pos + 4], "big")
        pos += 4
        if length <= 0 or pos + length > n:
            raise ValueError("truncated or invalid AVCC data")
        out += b"\x00\x00\x00\x01"
        out += data[pos : pos + length]
        pos += length
    if pos != n:
        raise ValueError("trailing bytes after AVCC NAL units")
    return bytes(out)


def _rbsp(nal: bytes) -> bytes:
    """Strip emulation-prevention bytes (``00 00 03`` -> ``00 00``)."""
    return bytes(nal).replace(b"\x00\x00\x03", b"\x00\x00")


class _BitReader:
    """Minimal bit reader with Exp-Golomb support."""

    __slots__ = ("_data", "_bit")

    def __init__(self, data: bytes):
        self._data = data
        self._bit = 0

    def u(self, count: int) -> int:
        if count < 0:
            raise ValueError("negative bit count")
        value = 0
        for _ in range(count):
            byte_index = self._bit >> 3
            if byte_index >= len(self._data):
                raise ValueError("end of RBSP")
            bit_index = 7 - (self._bit & 7)
            value = (value << 1) | ((self._data[byte_index] >> bit_index) & 1)
            self._bit += 1
        return value

    def ue(self) -> int:
        leading = 0
        while self.u(1) == 0:
            leading += 1
            if leading > 31:
                raise ValueError("Exp-Golomb code too long")
        if leading == 0:
            return 0
        return (1 << leading) - 1 + self.u(leading)


def _sps_high_fields(sps: bytes) -> tuple[int, int, int]:
    """Return (chroma_format_idc, bit_depth_luma_minus8, bit_depth_chroma_minus8).

    Defaults to (1, 0, 0) when the SPS is not a high-profile SPS or cannot be
    parsed.  ``sps`` is a complete NAL unit including its 1-byte header.
    """
    if len(sps) < 4:
        return 1, 0, 0
    try:
        rbsp = _rbsp(sps[1:])
        br = _BitReader(rbsp)
        br.u(8)  # profile_idc
        br.u(8)  # constraint_set flags + reserved
        br.u(8)  # level_idc
        br.ue()  # seq_parameter_set_id
        chroma = br.ue()
        if chroma == 3:
            br.u(1)  # separate_colour_plane_flag
        bit_depth_luma = br.ue()
        bit_depth_chroma = br.ue()
        if not (0 <= chroma <= 3):
            chroma = 1
        if not (0 <= bit_depth_luma <= 7):
            bit_depth_luma = 0
        if not (0 <= bit_depth_chroma <= 7):
            bit_depth_chroma = 0
        return chroma, bit_depth_luma, bit_depth_chroma
    except Exception:
        return 1, 0, 0


def build_avcc_record(sps: bytes, pps: bytes) -> bytes:
    """Build an AVCDecoderConfigurationRecord for one SPS and one PPS."""
    if len(sps) < 4:
        raise ValueError("SPS too short for an avcC record")
    if not pps:
        raise ValueError("empty PPS")

    record = bytearray()
    record += b"\x01"
    record += bytes(sps[1:4])
    record += b"\xff"  # lengthSizeMinusOne = 3 (4-byte NAL lengths)
    record += b"\xe1"  # numOfSequenceParameterSets = 1
    record += len(sps).to_bytes(2, "big")
    record += sps
    record += b"\x01"  # numOfPictureParameterSets
    record += len(pps).to_bytes(2, "big")
    record += pps

    if sps[1] in _HIGH_PROFILES:
        chroma, depth_luma, depth_chroma = _sps_high_fields(sps)
        record += bytes(
            (
                0xFC | (chroma & 0x03),
                0xF8 | (depth_luma & 0x07),
                0xF8 | (depth_chroma & 0x07),
                0x00,  # numOfSequenceParameterSetExt
            )
        )
    return bytes(record)


def codec_string(sps: bytes) -> str:
    """Return the WebCodecs/ISO codec string, e.g. ``avc1.640028``."""
    if len(sps) < 4:
        raise ValueError("SPS too short for a codec string")
    return "avc1.%02x%02x%02x" % (sps[1], sps[2], sps[3])


def parse_avcc_record(record: bytes) -> tuple[bytes, bytes]:
    """Extract the first SPS and PPS from an AVCDecoderConfigurationRecord."""
    if not record or record[0] != 1:
        raise ValueError("not an AVCDecoderConfigurationRecord")
    if len(record) < 7:
        raise ValueError("AVCDecoderConfigurationRecord too short")
    pos = 5
    num_sps = record[pos] & 0x1F
    pos += 1
    sps = None
    for _ in range(num_sps):
        if pos + 2 > len(record):
            raise ValueError("truncated SPS length")
        length = int.from_bytes(record[pos : pos + 2], "big")
        pos += 2
        if length == 0 or pos + length > len(record):
            raise ValueError("truncated SPS")
        if sps is None:
            sps = record[pos : pos + length]
        pos += length
    if pos >= len(record):
        raise ValueError("missing PPS count")
    num_pps = record[pos]
    pos += 1
    pps = None
    for _ in range(num_pps):
        if pos + 2 > len(record):
            raise ValueError("truncated PPS length")
        length = int.from_bytes(record[pos : pos + 2], "big")
        pos += 2
        if length == 0 or pos + length > len(record):
            raise ValueError("truncated PPS")
        if pps is None:
            pps = record[pos : pos + length]
        pos += length
    if sps is None or pps is None:
        raise ValueError("avcC record has no SPS/PPS")
    return sps, pps


def annexb_to_frame(
    data: bytes,
) -> tuple[bytes, bool, bytes | None, bytes | None]:
    """Turn one Annex B access unit into an AVCC access unit.

    Returns ``(avcc_bytes, is_idr, sps, pps)``.  SPS, PPS, AUD and SEI NAL
    units are removed from the payload; the first SPS/PPS seen are returned
    separately so callers can build/refresh the avcC description.
    ``is_idr`` is true when the access unit contains an IDR slice (type 5).
    """
    nals = split_annexb(data)
    sps: bytes | None = None
    pps: bytes | None = None
    kept: list[bytes] = []
    is_idr = False
    for nal in nals:
        ntype = nal_type(nal)
        if ntype == 7:
            if sps is None:
                sps = nal
            continue
        if ntype == 8:
            if pps is None:
                pps = nal
            continue
        if ntype in (6, 9):  # SEI / AUD are not part of the access unit payload
            continue
        if ntype == 5:
            is_idr = True
        kept.append(nal)
    return to_avcc(kept), is_idr, sps, pps
