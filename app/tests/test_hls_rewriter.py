"""Tests for the HLS playlist URI rewriter.

Three fixtures cover the cases we actually hit in the wild:
1. Master playlist with absolute URIs pointing at a CDN host.
2. Variant playlist with relative URIs (segments next to the playlist).
3. Playlist with #EXT-X-KEY URI attribute (AES-128 encrypted HLS).
"""

from castbooster.hls_rewriter import (
    decode_url,
    encode_url,
    rewrite_playlist,
)

PROXY_BASE = "http://192.168.1.42:38123"
TOKEN = "deadbeef"


def _extract_u_param(rewritten_uri: str) -> str:
    # rewritten_uri looks like http://192.168.1.42:38123/s/deadbeef/upstream/fetch.ts?u=<base64>
    assert "?u=" in rewritten_uri, rewritten_uri
    return rewritten_uri.split("?u=", 1)[1]


def test_master_playlist_absolute_uris():
    body = (
        "#EXTM3U\n"
        "#EXT-X-VERSION:4\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=1280000,RESOLUTION=1280x720\n"
        "https://cdn.example.com/v/720p/index.m3u8?token=aaa\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=640000,RESOLUTION=640x360\n"
        "https://cdn.example.com/v/360p/index.m3u8?token=aaa\n"
    )
    base = "https://cdn.example.com/v/master.m3u8?token=aaa"
    out = rewrite_playlist(body, base, TOKEN, PROXY_BASE)
    # Header lines untouched.
    assert "#EXTM3U" in out
    assert "#EXT-X-VERSION:4" in out
    assert "BANDWIDTH=1280000" in out
    # Both absolute URIs wrapped.
    lines = [l for l in out.splitlines() if l and not l.startswith("#")]
    assert len(lines) == 2
    for uri in lines:
        assert uri.startswith(f"{PROXY_BASE}/s/{TOKEN}/upstream/fetch.ts?u=")
    # Round-trip: decoding the first one gives back the original absolute URL.
    decoded = decode_url(_extract_u_param(lines[0]))
    assert decoded == "https://cdn.example.com/v/720p/index.m3u8?token=aaa"


def test_variant_playlist_relative_uris():
    body = (
        "#EXTM3U\n"
        "#EXT-X-VERSION:3\n"
        "#EXT-X-TARGETDURATION:10\n"
        "#EXT-X-MEDIA-SEQUENCE:0\n"
        "#EXTINF:10.0,\n"
        "seg-0.ts\n"
        "#EXTINF:10.0,\n"
        "seg-1.ts\n"
        "#EXTINF:8.0,\n"
        "seg-2.ts\n"
        "#EXT-X-ENDLIST\n"
    )
    base = "https://cdn.example.com/v/720p/index.m3u8?token=aaa"
    out = rewrite_playlist(body, base, TOKEN, PROXY_BASE)
    uris = [l for l in out.splitlines() if l and not l.startswith("#")]
    assert len(uris) == 3
    # Relative `seg-0.ts` should resolve to an absolute URL next to the playlist.
    decoded0 = decode_url(_extract_u_param(uris[0]))
    assert decoded0 == "https://cdn.example.com/v/720p/seg-0.ts"
    decoded2 = decode_url(_extract_u_param(uris[2]))
    assert decoded2 == "https://cdn.example.com/v/720p/seg-2.ts"
    # Tags preserved.
    assert "#EXT-X-ENDLIST" in out
    assert "#EXT-X-TARGETDURATION:10" in out


def test_playlist_with_ext_x_key_uri_attribute():
    body = (
        "#EXTM3U\n"
        "#EXT-X-VERSION:5\n"
        '#EXT-X-KEY:METHOD=AES-128,URI="https://cdn.example.com/keys/abc.key",IV=0x1a2b\n'
        "#EXT-X-MAP:URI=\"init.mp4\"\n"
        '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio-aac",NAME="English",DEFAULT=YES,URI="audio/en.m3u8"\n'
        "#EXTINF:6.0,\n"
        "fragments/00001.m4s\n"
        "#EXT-X-ENDLIST\n"
    )
    base = "https://cdn.example.com/v/enc/playlist.m3u8"
    out = rewrite_playlist(body, base, TOKEN, PROXY_BASE)

    # The #-lines with URI="..." attributes should have their attrs rewritten.
    key_line = next(l for l in out.splitlines() if l.startswith("#EXT-X-KEY"))
    assert 'URI="' + PROXY_BASE + '/s/' + TOKEN + '/upstream/fetch.ts?u=' in key_line
    key_u = key_line.split('URI="', 1)[1].split('"', 1)[0]
    assert decode_url(_extract_u_param(key_u)) == "https://cdn.example.com/keys/abc.key"

    map_line = next(l for l in out.splitlines() if l.startswith("#EXT-X-MAP"))
    map_u = map_line.split('URI="', 1)[1].split('"', 1)[0]
    assert decode_url(_extract_u_param(map_u)) == "https://cdn.example.com/v/enc/init.mp4"

    media_line = next(l for l in out.splitlines() if l.startswith("#EXT-X-MEDIA"))
    # Ensure the other attributes are still present.
    assert 'TYPE=AUDIO' in media_line
    assert 'GROUP-ID="audio-aac"' in media_line
    assert 'DEFAULT=YES' in media_line
    media_u = media_line.split('URI="', 1)[1].split('"', 1)[0]
    assert decode_url(_extract_u_param(media_u)) == "https://cdn.example.com/v/enc/audio/en.m3u8"

    # Segment line on its own.
    seg_line = next(l for l in out.splitlines() if l.endswith("m4s") is False and "/upstream/fetch.ts?u=" in l and not l.startswith("#"))
    seg_u = _extract_u_param(seg_line)
    assert decode_url(seg_u) == "https://cdn.example.com/v/enc/fragments/00001.m4s"


def test_encode_decode_roundtrip():
    urls = [
        "https://cdn.example.com/v/720p/index.m3u8?token=aaa&t=2",
        "https://example.com/path/with spaces/file.ts",
        "https://host/with?query=1&other=two",
    ]
    for u in urls:
        assert decode_url(encode_url(u)) == u


def test_blank_lines_preserved():
    body = "#EXTM3U\n\n#EXT-X-VERSION:3\n\nseg.ts\n"
    out = rewrite_playlist(body, "https://x/y/", TOKEN, PROXY_BASE)
    # Two blank lines in, two blank lines out. Relative 'seg.ts' rewritten.
    assert out.count("\n\n") >= 1
    assert "/upstream/fetch.ts?u=" in out


# ---------- Ad-segment filtering (2026-05-17) --------------------------------
#
# In the wild, pirate streaming aggregators (e.g. masukestin.com) inject
# pre-roll ad segments pointing at third-party CDNs that return PNG images
# despite the playlist's EXTINF claiming a 10-second video segment. The
# Chromecast's native HLS player tolerates this and skips ahead; ffmpeg's TS
# demuxer chokes on the PNG bytes and exits with "Invalid data found when
# processing input" (-> input_unreachable in the transcoder). Filtering those
# segments out during rewrite lets the transcoder run.


def test_rewrite_filters_tiktokcdn_ad_segment():
    body = (
        "#EXTM3U\n"
        "#EXT-X-VERSION:3\n"
        "#EXT-X-TARGETDURATION:10\n"
        "#EXT-X-MEDIA-SEQUENCE:1\n"
        "#EXTINF:10.000,\n"
        "https://p16-ad-site-sign-sg.tiktokcdn.com/ad-site-i18n-sg/abc~tplv.image\n"
        "#EXTINF:10.000,\n"
        "https://cdn.example.com/v/720p/seg-1.ts\n"
        "#EXT-X-ENDLIST\n"
    )
    base = "https://masukestin.com/stream/index.m3u8"
    out = rewrite_playlist(body, base, TOKEN, PROXY_BASE)
    # The tiktokcdn ad URI must not appear in the rewritten output.
    assert "tiktokcdn" not in out
    # The real segment must still be there, wrapped.
    real_lines = [l for l in out.splitlines() if not l.startswith("#") and l.strip()]
    assert len(real_lines) == 1
    assert decode_url(_extract_u_param(real_lines[0])) == "https://cdn.example.com/v/720p/seg-1.ts"


def test_rewrite_does_not_filter_image_extensions_under_unknown_domain():
    """Regression guard for 2026-05-18 audinifer.com: extensions alone
    (.png/.jpg/.gif/.webp/.bmp/.svg) MUST NOT trigger filtering — pirate
    streaming sites disguise legitimate video segments under image
    extensions to evade ad-blocker URL filters. The previous heuristic
    iteration matched every audinifer.com segment by extension and zeroed
    the playlist; this test pins the policy that extensions alone don't
    count, only known ad-network markers do."""
    body = (
        "#EXTM3U\n"
        "#EXT-X-VERSION:3\n"
        "#EXTINF:5.0,\n"
        "https://cdn.example.com/v/seg-0.png\n"
        "#EXTINF:5.0,\n"
        "https://cdn.example.com/v/seg-1.jpg\n"
        "#EXTINF:5.0,\n"
        "https://cdn.example.com/v/seg-2.webp\n"
    )
    out = rewrite_playlist(body, "https://cdn.example.com/v/", TOKEN, PROXY_BASE)
    uris = [l for l in out.splitlines() if not l.startswith("#") and l.strip()]
    # All three segments must survive — none of the extensions are markers.
    assert len(uris) == 3


def test_rewrite_falls_back_when_all_segments_match_heuristic():
    """Safety net for an over-broad heuristic: when every segment matches,
    skip filtering and pass the playlist through. Better to deliver ads (or
    let ffmpeg fail on individual segments) than to wipe the playlist to
    zero and break the cast entirely. Regression guard for the 2026-05-18
    audinifer.com incident."""
    body = (
        "#EXTM3U\n"
        "#EXT-X-MEDIA-SEQUENCE:1\n"
        "#EXTINF:10.0,\n"
        "https://p16-ad-site-sign-sg.tiktokcdn.com/a.image\n"
        "#EXTINF:10.0,\n"
        "https://p16-ad-site-sign-sg.tiktokcdn.com/b.image\n"
        "#EXT-X-ENDLIST\n"
    )
    out = rewrite_playlist(body, "https://x/y/", TOKEN, PROXY_BASE)
    uris = [l for l in out.splitlines() if not l.startswith("#") and l.strip()]
    # Both ad URIs preserved (URL-wrapped) — fallback skipped filtering.
    assert len(uris) == 2
    # MEDIA-SEQUENCE not bumped.
    assert "#EXT-X-MEDIA-SEQUENCE:1" in out


def test_rewrite_drops_preceding_extinf_when_dropping_ad():
    """The #EXTINF tag immediately preceding a dropped URI must also be
    dropped — otherwise ffmpeg would see an EXTINF with no following URI,
    which is a malformed playlist."""
    body = (
        "#EXTM3U\n"
        "#EXT-X-VERSION:3\n"
        "#EXT-X-MEDIA-SEQUENCE:0\n"
        "#EXTINF:10.0,ad-marker\n"
        "https://p16-ad-site-sign-sg.tiktokcdn.com/x.image\n"
        "#EXTINF:10.0,\n"
        "real-seg.ts\n"
    )
    base = "https://cdn.example.com/v/360p/index.m3u8"
    out = rewrite_playlist(body, base, TOKEN, PROXY_BASE)
    extinf_count = sum(1 for l in out.splitlines() if l.startswith("#EXTINF"))
    # Exactly one #EXTINF should remain — the one for the real segment.
    assert extinf_count == 1, out
    assert "ad-marker" not in out


def test_rewrite_increments_media_sequence_for_leading_ads():
    """If the first N segments are dropped as ads, MEDIA-SEQUENCE must be
    bumped by N so the kept segments retain their correct sequence numbers."""
    body = (
        "#EXTM3U\n"
        "#EXT-X-VERSION:3\n"
        "#EXT-X-TARGETDURATION:10\n"
        "#EXT-X-MEDIA-SEQUENCE:5\n"
        "#EXTINF:10.0,\n"
        "https://p16-ad-site-sign-sg.tiktokcdn.com/a~.image\n"
        "#EXTINF:10.0,\n"
        "https://p16-ad-site-sign-sg.tiktokcdn.com/b~.image\n"
        "#EXTINF:10.0,\n"
        "https://cdn.example.com/real-seg.ts\n"
    )
    base = "https://cdn.example.com/v/playlist.m3u8"
    out = rewrite_playlist(body, base, TOKEN, PROXY_BASE)
    # 5 + 2 dropped leading ads -> 7
    seq_line = next(l for l in out.splitlines() if l.startswith("#EXT-X-MEDIA-SEQUENCE"))
    assert seq_line.strip() == "#EXT-X-MEDIA-SEQUENCE:7"


def test_rewrite_does_not_touch_media_sequence_when_ad_is_not_leading():
    """If the only dropped ad is in the middle/end, leading segments are
    intact — leave MEDIA-SEQUENCE alone."""
    body = (
        "#EXTM3U\n"
        "#EXT-X-MEDIA-SEQUENCE:1\n"
        "#EXTINF:10.0,\n"
        "https://cdn.example.com/seg-1.ts\n"
        "#EXTINF:10.0,\n"
        "https://p16-ad-site-sign-sg.tiktokcdn.com/x.image\n"
        "#EXTINF:10.0,\n"
        "https://cdn.example.com/seg-2.ts\n"
    )
    base = "https://cdn.example.com/v/playlist.m3u8"
    out = rewrite_playlist(body, base, TOKEN, PROXY_BASE)
    seq_line = next(l for l in out.splitlines() if l.startswith("#EXT-X-MEDIA-SEQUENCE"))
    assert seq_line.strip() == "#EXT-X-MEDIA-SEQUENCE:1"


def test_rewrite_preserves_normal_ts_segments():
    """No false positives: a plain .ts URI under a normal CDN must not be
    classified as an ad."""
    body = (
        "#EXTM3U\n"
        "#EXTINF:10.0,\n"
        "https://video.cdn.example.com/720p/seg-0.ts\n"
        "#EXTINF:10.0,\n"
        "https://video.cdn.example.com/720p/seg-1.ts\n"
    )
    out = rewrite_playlist(body, "https://x/y/", TOKEN, PROXY_BASE)
    uris = [l for l in out.splitlines() if not l.startswith("#") and l.strip()]
    assert len(uris) == 2
