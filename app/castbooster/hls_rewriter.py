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
import re
from urllib.parse import urljoin

# Attributes in #EXT-X-* tags that carry URIs we must rewrite.
# Examples:
#   #EXT-X-KEY:METHOD=AES-128,URI="key.bin",IV=0x...
#   #EXT-X-MAP:URI="init.mp4",BYTERANGE="800@0"
#   #EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="en",URI="audio.m3u8"
# Single regex handles all of them.
_URI_ATTR_RE = re.compile(r'(URI=")([^"]+)(")')


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
    """
    out = []
    for raw in body.splitlines():
        # Normalize trailing CRs but preserve otherwise.
        line = raw.rstrip("\r")
        stripped = line.strip()
        if not stripped:
            out.append(line)
            continue
        if stripped.startswith("#"):
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
