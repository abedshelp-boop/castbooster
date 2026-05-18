"""HLS/DASH playlist URI rewriter.

The upstream CDN is session-locked: its URLs need the original browser's
cookies + User-Agent. When a Chromecast fetches our proxy URL, the Chromecast's
network identity is wrong. So we:

1. Receive a request for `http://<LAN-IP>:38123/s/<token>/master.m3u8`.
2. Fetch the real `sess.upstream_url` (the one the browser saw) with the
   captured cookies/headers.
3. Rewrite every URI in the playlist so it points back through this proxy
   at `/s/<token>/fetch?u=<base64url>`, where the base64url payload is the
   absolute upstream URL.
4. When the Chromecast fetches one of those rewritten URLs, we do the same
   session-aware fetch for the segment and stream the bytes back.

This module is the pure-text part: given a playlist body + base URL + token +
proxy base, produce the rewritten body. Network I/O lives in proxy.py.
"""

from __future__ import annotations

import base64
import logging
import re
from urllib.parse import urljoin

log = logging.getLogger(__name__)

# Attributes in #EXT-X-* tags that carry URIs we must rewrite.
# Examples:
#   #EXT-X-KEY:METHOD=AES-128,URI="key.bin",IV=0x...
#   #EXT-X-MAP:URI="init.mp4",BYTERANGE="800@0"
#   #EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="en",URI="audio.m3u8"
# Single regex handles all of them.
_URI_ATTR_RE = re.compile(r'(URI=")([^"]+)(")')

# Ad-segment filtering — deliberately conservative.
#
# 2026-05-17: masukestin.com playlists inject pre-roll "segments" pointing at
# TikTok-CDN ad images. ffmpeg's TS demuxer chokes on the PNG bytes; the
# Chromecast player just skips ahead. We drop those segments during rewrite
# so the transcoder pipeline can run end-to-end.
#
# 2026-05-18 (this file's previous iteration): an over-broad heuristic that
# also matched `.png/.jpg/.gif/.webp/.bmp/.svg` extensions wiped the entire
# audinifer.com playlist (139/139 segments dropped). Some pirate streaming
# sites disguise legitimate video segments under image extensions to evade
# ad-blocker URL filters, so extension-based matching is unsafe.
#
# Lesson: missing an ad costs 1 segment of ffmpeg decode error (recoverable);
# false-matching a real segment empties the playlist and breaks the cast.
# Markers below must be highly specific to ad networks — not video formats.
_AD_MARKERS = (
    "tiktokcdn.com",           # TikTok CDN — the observed pre-roll ad source
    "/ad-site-",               # TikTok-specific path
    "doubleclick.net",         # Google Ads
    "googlesyndication.com",   # Google Ads
)


def _is_likely_ad_segment(absolute_url: str) -> bool:
    """Return True if `absolute_url` matches a known ad-network marker.
    Pattern check only — no network I/O. See `_AD_MARKERS` for the policy
    on what counts as an ad."""
    if not absolute_url:
        return False
    low = absolute_url.lower()
    return any(marker in low for marker in _AD_MARKERS)


def encode_url(absolute_url: str) -> str:
    """base64url-encode without padding — URL-safe for query strings."""
    return (
        base64.urlsafe_b64encode(absolute_url.encode("utf-8"))
        .decode("ascii")
        .rstrip("=")
    )


def decode_url(encoded: str) -> str:
    pad = "=" * (-len(encoded) % 4)
    return base64.urlsafe_b64decode((encoded + pad).encode("ascii")).decode("utf-8")


def _wrap(uri: str, base_url: str, token: str, proxy_base: str) -> str:
    if not uri.strip():
        return uri
    absolute = urljoin(base_url, uri)
    # P2.4: route is /s/<token>/upstream/fetch.ts. The literal `.ts` suffix
    # satisfies ffmpeg's HLS demuxer allowed_segment_extensions check
    # (modern ffmpeg refuses to fetch segments without a recognized extension
    # in the URL path; query params don't count). Our handler ignores the
    # suffix and reads the absolute URL from the `u` query param.
    return f"{proxy_base}/s/{token}/upstream/fetch.ts?u={encode_url(absolute)}"


def rewrite_playlist(body: str, base_url: str, token: str, proxy_base: str) -> str:
    """Walk playlist lines. Non-'#' lines are URIs. '#'-lines may carry URI="..."
    attributes. Everything else passes through verbatim so we preserve fidelity.

    Also filters out injected ad "segments" (see `_is_likely_ad_segment`) and
    their preceding #EXTINF tag. If any leading ad segments are dropped, the
    #EXT-X-MEDIA-SEQUENCE tag is incremented so kept segments retain their
    correct sequence numbers.
    """
    raw_lines = [r.rstrip("\r") for r in body.splitlines()]

    # Pass 1: classify each plain-URI line as keep or ad-drop.
    uri_indices: list[int] = []
    ad_uri_indices: list[int] = []
    for i, raw in enumerate(raw_lines):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        uri_indices.append(i)
        absolute = urljoin(base_url, stripped)
        if _is_likely_ad_segment(absolute):
            ad_uri_indices.append(i)

    drop_indices: set[int] = set()
    leading_ads_dropped = 0

    # Safety net: if every segment in the playlist matches the heuristic,
    # the heuristic is over-broad — filtering would leave zero playable
    # segments and break the cast entirely. Skip filtering and warn with a
    # sample so we can investigate. Regression guard for 2026-05-18 audinifer.com.
    filter_safe = bool(ad_uri_indices) and len(ad_uri_indices) < len(uri_indices)
    if ad_uri_indices and not filter_safe:
        sample = raw_lines[ad_uri_indices[0]].strip()[:200]
        log.warning(
            "ad-filter matched all %d segments (over-broad heuristic) — "
            "passing playlist through unfiltered; sample: %s",
            len(uri_indices), sample,
        )

    if filter_safe:
        ad_idx_set = set(ad_uri_indices)
        seen_real_segment = False
        for idx in uri_indices:
            if idx in ad_idx_set:
                drop_indices.add(idx)
                j = idx - 1
                while j >= 0:
                    prev = raw_lines[j].strip()
                    if not prev:
                        break
                    if prev.startswith("#EXTINF") or prev.startswith("#EXT-X-BYTERANGE"):
                        drop_indices.add(j)
                        j -= 1
                    else:
                        break
                if not seen_real_segment:
                    leading_ads_dropped += 1
            else:
                seen_real_segment = True
        log.info(
            "ad-filter dropped %d/%d segments (leading=%d)",
            len(ad_uri_indices), len(uri_indices), leading_ads_dropped,
        )

    out: list[str] = []
    for i, raw in enumerate(raw_lines):
        if i in drop_indices:
            continue
        line = raw
        stripped = line.strip()
        if not stripped:
            out.append(line)
            continue
        if stripped.startswith("#"):
            if leading_ads_dropped > 0 and stripped.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                try:
                    current = int(stripped.split(":", 1)[1].strip())
                    line = f"#EXT-X-MEDIA-SEQUENCE:{current + leading_ads_dropped}"
                except (IndexError, ValueError):
                    pass
            else:
                line = _URI_ATTR_RE.sub(
                    lambda m: m.group(1) + _wrap(m.group(2), base_url, token, proxy_base) + m.group(3),
                    line,
                )
            out.append(line)
            continue
        # Plain URI line.
        out.append(_wrap(stripped, base_url, token, proxy_base))
    # Preserve trailing newline — most HLS parsers tolerate either, but some
    # older Chromecast firmwares are stricter.
    return "\n".join(out) + "\n"
