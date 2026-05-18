"""Tests for detect_png_ts_wrapper_offset — the anti-adblocker PNG/TS unwrap.

2026-05-18: discovered in production that masukestin/hanerix/audinifer's
upstream CDN prefixes MPEG-TS segments with a ~62-byte fake PNG header to
disguise them as image content. The Chromecast's HLS player tolerantly
skips past the PNG header to the TS sync byte; ffmpeg's strict TS demuxer
bails with 'Invalid data found when processing input'. These tests pin the
detection heuristic — false positives (stripping a real PNG/JPEG that
happens to contain 0x47 bytes) would corrupt unrelated image responses, so
the heuristic must require three independent signals to fire.
"""
from __future__ import annotations

from castbooster.proxy import detect_png_ts_wrapper_offset


_PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _build_minimal_png_header() -> tuple[bytes, int]:
    """Build the shortest plausible PNG that ends with an IEND chunk.

    Layout: 8-byte signature + IHDR (4 length + 4 type + 13 data + 4 CRC)
    + IEND (4 length + 4 type + 4 CRC). Returns (bytes, offset_after_IEND).
    """
    ihdr_len = (13).to_bytes(4, "big")
    ihdr = b"IHDR" + b"\x00" * 13 + b"\x00\x00\x00\x00"  # type + data + CRC
    iend_len = (0).to_bytes(4, "big")
    iend = b"IEND" + b"\x00\x00\x00\x00"                   # type + CRC
    blob = _PNG_SIG + ihdr_len + ihdr + iend_len + iend
    # offset where TS data would start: right after IEND's CRC
    return blob, len(blob)


def _ts_packet(payload_byte: int = 0xFF) -> bytes:
    """One synthetic 188-byte MPEG-TS packet, starting with the sync byte 0x47."""
    return b"\x47" + bytes([payload_byte]) * 187


# -------- positive cases (real wrapped segments) --------


def test_detects_wrapper_when_png_then_ts():
    blob, offset = _build_minimal_png_header()
    # Append two TS packets to satisfy the two-sync-byte confirmation.
    wrapped = blob + _ts_packet() + _ts_packet()
    assert detect_png_ts_wrapper_offset(wrapped) == offset


def test_detects_wrapper_matching_real_world_offset():
    """Production data: bytes after IEND at offset 62 looked like
    47 40 00 16 ... (a TS PAT packet). Reproduce that shape."""
    blob, offset = _build_minimal_png_header()
    ts1 = b"\x47\x40\x00\x16" + bytes(184)
    ts2 = b"\x47" + bytes(187)
    wrapped = blob + ts1 + ts2
    result = detect_png_ts_wrapper_offset(wrapped)
    assert result == offset
    # The byte at the returned offset must be the TS sync.
    assert wrapped[result] == 0x47


# -------- negative cases (legitimate content) --------


def test_no_wrapper_on_empty():
    assert detect_png_ts_wrapper_offset(b"") == 0


def test_no_wrapper_on_short_random_bytes():
    assert detect_png_ts_wrapper_offset(b"hello world") == 0


def test_no_wrapper_on_mpegts_without_png_header():
    """Plain TS stream — no PNG signature, must not match."""
    plain_ts = _ts_packet() + _ts_packet()
    assert detect_png_ts_wrapper_offset(plain_ts) == 0


def test_no_wrapper_on_real_png_without_ts_appended():
    """A real PNG image (no MPEG-TS data after IEND) must NOT be stripped.
    Otherwise we'd corrupt legitimate image responses."""
    blob, _offset = _build_minimal_png_header()
    # Real PNG could have any non-0x47 bytes after IEND (or end of file).
    real_png = blob + b"\x00\x01\x02\x03\x89PNG-extra-junk"
    assert detect_png_ts_wrapper_offset(real_png) == 0


def test_no_wrapper_on_png_with_isolated_0x47():
    """A real PNG that happens to have a 0x47 right after IEND must still
    not match — the second-sync confirmation at +188 catches this."""
    blob, _offset = _build_minimal_png_header()
    # 0x47 immediately after IEND, but not at +188 (random bytes there).
    fake = blob + b"\x47" + b"X" * 200
    assert detect_png_ts_wrapper_offset(fake) == 0


def test_no_wrapper_on_jpeg_with_ts_bytes():
    """Even if some unrelated image format contains 0x47 bytes, the PNG-
    signature check is the first gate — no PNG signature, no detection."""
    jpeg_like = b"\xff\xd8\xff\xe0" + b"\x47" * 1000
    assert detect_png_ts_wrapper_offset(jpeg_like) == 0


def test_no_wrapper_when_iend_far_beyond_scan_limit():
    """We don't scan the whole file for IEND — only the first 4096 bytes.
    Pathological PNG with chunks past that limit isn't matched."""
    blob = _PNG_SIG + b"\x00" * 5000 + b"IEND" + b"\x00" * 4 + b"\x47\x47\x47"
    assert detect_png_ts_wrapper_offset(blob) == 0
