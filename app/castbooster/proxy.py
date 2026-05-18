import asyncio
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional
from urllib.parse import urlparse

import yarl
from aiohttp import ClientSession, ClientTimeout, web

from castbooster import __version__
from castbooster import ffmpeg_probe
from castbooster.caster import CastManager
from castbooster.ffmpeg_probe import (
    AccelProfile, FFmpegNotFoundError, FFmpegProbeError,
)
from castbooster.filter_chain import FilterChain, NoopFilter
from castbooster.hls_rewriter import decode_url, rewrite_playlist
from castbooster.netinfo import get_lan_ip
from castbooster.output_dir_sweep import sweep_stranded_output_dirs
from castbooster.session_store import SessionStore, StreamSession
from castbooster.transcoder import Transcoder, TranscoderState

log = logging.getLogger(__name__)

PROXY_HOST = "0.0.0.0"
PROXY_PORT = 38123

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

# Headers we forward from the Chromecast's request up to the CDN.
_FORWARD_REQ_HEADERS = ("range",)
# Headers we copy from the CDN response back to the Chromecast.
_FORWARD_RESP_HEADERS = (
    "content-type",
    "content-length",
    "content-range",
    "accept-ranges",
    "cache-control",
    "etag",
    "last-modified",
)

_PLAYLIST_CONTENT_TYPES = ("mpegurl", "dash+xml")

# Chromecast's Default Media Receiver loads from gstatic.com and fetches
# our proxied URLs via MSE from a different origin. Without these CORS
# headers the receiver's Chromium sandbox silently rejects the response
# and the TV spins forever.
_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
    "Access-Control-Allow-Headers": "*",
    "Access-Control-Expose-Headers": "*",
}

# P2.4: states in which /output/* may serve from disk. RELOADING is alive
# — OLD slot is still streaming while NEW is warming. NEVER 503 RELOADING.
# This applies the 2026-04-22 IDLE-race lesson to P2.3's new state.
_TRANSCODER_ALIVE_STATES = frozenset({
    TranscoderState.READY,
    TranscoderState.STREAMING,
    TranscoderState.STALLED,
    TranscoderState.RELOADING,
})


# P2.4: per-tier READY budget. Hardware encoders write first segment <= 4s;
# software needs more headroom. See spec D2.
_WARMING_TIMEOUT_BY_TIER = {
    "nvidia": 6.0,
    "intel":  6.0,
    "amd":    6.0,
    "sw":     12.0,
}

_PASSTHROUGH_ENV_TRUTHY = {"1", "true", "yes"}


def _is_passthrough_env_set() -> bool:
    return os.environ.get("CASTBOOSTER_PASSTHROUGH", "").strip().lower() in _PASSTHROUGH_ENV_TRUTHY


def _passthrough_playback_url(lan_ip: str, sess: StreamSession) -> tuple[str, str]:
    """Build the /upstream/{cosmetic-suffix} playback URL + content-type."""
    path_seg, content_type = _guess_manifest_path(sess.upstream_url)
    url = f"http://{lan_ip}:{PROXY_PORT}/s/{sess.token}/upstream/{path_seg}"
    return url, content_type


def _output_playback_url(lan_ip: str, sess: StreamSession) -> tuple[str, str]:
    """Build the /output/master.m3u8 playback URL — always HLS."""
    url = f"http://{lan_ip}:{PROXY_PORT}/s/{sess.token}/output/master.m3u8"
    return url, "application/vnd.apple.mpegurl"


NMHandler = Callable[[web.Application, dict], Awaitable[dict]]


class ProxyHandle:
    """Holds references to the proxy loop + shutdown event so the main thread
    can signal a clean shutdown from outside the loop."""

    def __init__(self) -> None:
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._shutdown: Optional[asyncio.Event] = None
        self.thread: Optional[threading.Thread] = None
        self.lan_ip: str = ""
        # Signalled by the proxy thread once TCPSite.start() has bound the
        # port (success) OR the run crashed (failure). Main thread blocks on
        # this briefly so we can exit if bind failed instead of running a
        # tray icon with no proxy behind it.
        self._ready: threading.Event = threading.Event()
        self.bind_failed: bool = False

    def wait_ready(self, timeout: float) -> bool:
        """Block until the proxy has bound the port or failed trying.
        Returns True if bound successfully, False on timeout or bind failure.
        """
        if not self._ready.wait(timeout):
            return False
        return not self.bind_failed

    def stop(self) -> None:
        if self.loop and self._shutdown and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self._shutdown.set)
        if self.thread:
            self.thread.join(timeout=5)


async def _handle_ping(app: web.Application, _msg: dict) -> dict:
    return {"type": "pong", "version": __version__, "lanIp": app["lan_ip"]}


async def _handle_not_implemented(_app: web.Application, msg: dict) -> dict:
    return {
        "type": "error",
        "detail": f"'{msg.get('type')}' not implemented yet",
    }


async def _handle_list_casts(app: web.Application, _msg: dict) -> dict:
    cm: CastManager = app["cast_manager"]
    loop = asyncio.get_running_loop()
    casts = await loop.run_in_executor(None, cm.list_devices)
    return {"type": "casts", "casts": casts}


async def _handle_cast(app: web.Application, msg: dict) -> dict:
    """Spawn (or skip) a transcoder, READY-gate, fall back on FAILED.

    See spec docs/superpowers/specs/2026-05-15-pillar-2.4-proxy-integration-design.md §3.2.
    """
    token = msg.get("token")
    cast_uuid = msg.get("castUuid")
    if not token or not isinstance(token, str):
        return {"type": "casting", "status": "error", "detail": "missing 'token'"}
    if not cast_uuid or not isinstance(cast_uuid, str):
        return {"type": "casting", "status": "error", "detail": "missing 'castUuid'"}
    store: SessionStore = app["session_store"]
    sess = store.get(token)
    if sess is None:
        return {"type": "casting", "status": "error", "detail": "unknown token"}

    cm: CastManager = app["cast_manager"]
    log_token = token[:8]
    log.info(
        "[cast token=%s] start uuid=%s upstream=%s",
        log_token, cast_uuid, sess.upstream_url[:120],
    )
    # ALL blocking Transcoder/CastManager calls below MUST be offloaded via
    # this executor — see spec §3.9.  Synchronously waiting on the transcoder
    # would freeze the aiohttp event loop and prevent ffmpeg from fetching
    # /upstream/master.m3u8 through the proxy, causing warming_timed_out on
    # every cast.  (2026-05-16 root cause for the P2.4 acceptance regression.)
    loop = asyncio.get_running_loop()

    # Step 2-3: passthrough decision
    env_passthrough = _is_passthrough_env_set()
    if env_passthrough:
        log.info("[cast token=%s] env CASTBOOSTER_PASSTHROUGH=1; passthrough forced", log_token)
        sess.passthrough_only = True
    accel = app.get("accel_profile")
    if accel is None and not sess.passthrough_only:
        log.warning("[cast token=%s] no AccelProfile available; forcing passthrough", log_token)
        sess.passthrough_only = True

    # Step 4: maybe spawn the transcoder
    if not sess.passthrough_only:
        warming_timeout = _WARMING_TIMEOUT_BY_TIER.get(accel.tier, 12.0)
        base_output_dir = Path(tempfile.gettempdir()) / "castbooster" / token
        upstream_loopback_url = (
            f"http://127.0.0.1:{PROXY_PORT}/s/{token}/upstream/master.m3u8"
        )
        log.info(
            "[cast token=%s] spawning transcoder accel=%s/%s/%s budget=%.1fs base=%s",
            log_token, accel.encoder, accel.decoder, accel.tier,
            warming_timeout, base_output_dir,
        )
        transcoder = Transcoder(
            input_url=upstream_loopback_url,
            base_output_dir=base_output_dir,
            accel=accel,
            filter_chain=FilterChain([NoopFilter()]),
            warming_timeout=warming_timeout,
        )
        sess.transcoder = transcoder
        sess.output_dir = base_output_dir
        try:
            await loop.run_in_executor(None, transcoder.start)
            log.info("[cast token=%s] transcoder WARMING", log_token)
            t0 = time.monotonic()
            ready = await loop.run_in_executor(
                None, transcoder.wait_until_ready, warming_timeout
            )
            elapsed = time.monotonic() - t0
        except Exception as e:
            log.exception("[cast token=%s] transcoder.start crashed", log_token)
            ready = False
            elapsed = 0.0
            try:
                await loop.run_in_executor(None, transcoder.stop)
            except Exception:
                log.exception("[cast token=%s] transcoder.stop after crash failed", log_token)
            sess.transcoder = None
            sess.output_dir = None
            sess.passthrough_only = True
            cm.record_transcoder_failure(f"start_exception:{type(e).__name__}")
        if ready:
            log.info(
                "[cast token=%s] transcoder READY in %.2fs (slot=v1)",
                log_token, elapsed,
            )
        elif sess.passthrough_only:
            # Already fell back above due to start-crash.
            pass
        else:
            reason = transcoder.idle_reason or "warming_timed_out"
            log.warning(
                "[cast token=%s] transcoder FAILED reason=%s elapsed=%.2fs",
                log_token, reason, elapsed,
            )
            cm.record_transcoder_failure(reason)
            stats = cm.transcoder_failure_stats()
            log.warning(
                "[cast token=%s] failure counter: total=%d by_reason=%s",
                log_token, stats["total"], stats["by_reason"],
            )
            try:
                await loop.run_in_executor(None, transcoder.stop)
            except Exception:
                log.exception("[cast token=%s] transcoder.stop after FAILED failed", log_token)
            sess.transcoder = None
            sess.output_dir = None
            sess.passthrough_only = True
            log.info(
                "[cast token=%s] falling back to passthrough; session.passthrough_only=True",
                log_token,
            )

    # Step 5-6: build playback URL + on_session_end + dispatch
    if sess.passthrough_only:
        playback_url, content_type = _passthrough_playback_url(app["lan_ip"], sess)
    else:
        playback_url, content_type = _output_playback_url(app["lan_ip"], sess)

    def _on_session_end() -> None:
        t = sess.transcoder
        if t is not None and t.state not in (
            TranscoderState.TERMINATING, TranscoderState.TERMINATED,
        ):
            try:
                t.stop()
            except Exception:
                log.exception("[cast token=%s] on_session_end transcoder.stop failed", log_token)
        sess.transcoder = None

    try:
        name = await loop.run_in_executor(
            None,
            lambda: cm.play(
                cast_uuid, playback_url, content_type,
                on_session_end=_on_session_end,
                log_token=token,
            ),
        )
    except LookupError as e:
        return {"type": "casting", "status": "error", "detail": str(e)}
    except Exception as e:
        log.exception("[cast token=%s] cm.play failed", log_token)
        return {
            "type": "casting", "status": "error",
            "detail": f"play_media failed: {e}",
        }
    log.info("[cast token=%s] play_media → %s", log_token, playback_url)
    return {
        "type": "casting",
        "status": "ok",
        "detail": f"Playback started on {name}",
        "playbackUrl": playback_url,
    }


def _guess_manifest_path(url: str) -> tuple[str, str]:
    """Return (path_segment, content_type) based on URL shape.

    path_segment is what lives after the session token in the playback URL,
    e.g. master.m3u8 / manifest.mpd / video.mp4. Keeping the extension right
    helps Chromecast sniff the format when our Content-Type header gets lost.
    """
    lower = url.lower()
    if ".m3u8" in lower or ".urlset/" in lower:
        return "master.m3u8", "application/vnd.apple.mpegurl"
    if ".mpd" in lower:
        return "manifest.mpd", "application/dash+xml"
    if ".webm" in lower:
        return "video.webm", "video/webm"
    if ".mp4" in lower:
        return "video.mp4", "video/mp4"
    return "video.mp4", "video/mp4"


async def _handle_register_stream(app: web.Application, msg: dict) -> dict:
    url = msg.get("url")
    if not isinstance(url, str) or not url:
        return {"type": "error", "detail": "missing 'url'"}
    cookies = msg.get("cookies") or []
    headers = msg.get("headers") or {}
    user_agent = msg.get("userAgent") or ""
    if not isinstance(cookies, list):
        return {"type": "error", "detail": "'cookies' must be a list"}
    if not isinstance(headers, dict):
        return {"type": "error", "detail": "'headers' must be an object"}

    store: SessionStore = app["session_store"]
    sess = store.create(url, cookies, headers, user_agent)
    path_seg, content_type = _guess_manifest_path(url)
    playback_url = (
        f"http://{app['lan_ip']}:{PROXY_PORT}/s/{sess.token}/upstream/{path_seg}"
    )
    return {
        "type": "stream_registered",
        "token": sess.token,
        "playbackUrl": playback_url,
        "contentType": content_type,
    }


async def _handle_media_cmd(app: web.Application, msg: dict) -> dict:
    uuid = msg.get("castUuid")
    action = msg.get("action")
    if not uuid or not isinstance(uuid, str):
        return {"type": "media_cmd_result", "status": "error", "detail": "missing castUuid"}
    if not action or not isinstance(action, str):
        return {"type": "media_cmd_result", "status": "error", "detail": "missing action"}
    cm: CastManager = app["cast_manager"]
    loop = asyncio.get_running_loop()
    seconds = msg.get("seconds")
    delta = msg.get("delta")
    volume = msg.get("volume")
    try:
        await loop.run_in_executor(
            None,
            lambda: cm.control(uuid, action, seconds=seconds, delta=delta, volume=volume),
        )
    except LookupError as e:
        return {"type": "media_cmd_result", "status": "error", "detail": str(e)}
    except ValueError as e:
        return {"type": "media_cmd_result", "status": "error", "detail": str(e)}
    except Exception as e:
        log.exception("media_cmd failed: action=%s uuid=%s", action, uuid)
        return {"type": "media_cmd_result", "status": "error", "detail": str(e)}
    return {"type": "media_cmd_result", "status": "ok"}


async def _handle_media_status(app: web.Application, msg: dict) -> dict:
    uuid = msg.get("castUuid")
    if not uuid or not isinstance(uuid, str):
        return {
            "type": "media_status_result",
            "state": "IDLE",
            "currentTime": 0,
            "duration": 0,
            "title": "",
            "canSeek": False,
            "error": "missing castUuid",
        }
    cm: CastManager = app["cast_manager"]
    loop = asyncio.get_running_loop()
    try:
        status = await loop.run_in_executor(None, cm.get_status, uuid)
    except LookupError:
        # No active session — popup interprets this as "cast ended".
        return {
            "type": "media_status_result",
            "state": "IDLE",
            "currentTime": 0,
            "duration": 0,
            "title": "",
            "canSeek": False,
            "castUuid": uuid,
        }
    except Exception as e:
        log.exception("media_status failed: uuid=%s", uuid)
        return {
            "type": "media_status_result",
            "state": "IDLE",
            "currentTime": 0,
            "duration": 0,
            "title": "",
            "canSeek": False,
            "castUuid": uuid,
            "error": str(e),
        }
    status["type"] = "media_status_result"
    status["castUuid"] = uuid
    # Device name lookup — handy for the popup title/subtitle.
    try:
        devices = await loop.run_in_executor(None, cm.list_devices)
        for d in devices:
            if d.get("uuid") == uuid:
                status["deviceName"] = d.get("name", "")
                break
    except Exception:
        log.debug("device lookup during media_status failed", exc_info=True)
    return status


def _default_handlers() -> Dict[str, NMHandler]:
    return {
        "ping": _handle_ping,
        "register_stream": _handle_register_stream,
        "list_casts": _handle_list_casts,
        "cast": _handle_cast,
        "media_cmd": _handle_media_cmd,
        "media_status": _handle_media_status,
    }


async def _health(request: web.Request) -> web.Response:
    return web.json_response(
        {"ok": True, "version": __version__, "lanIp": request.app["lan_ip"]}
    )


def _cookies_for_host(cookie_list, host: str) -> Dict[str, str]:
    """Filter the session's cookie list to only those whose domain matches
    `host` (exact or parent-domain). Returns a name->value dict suitable for
    aiohttp.ClientSession's `cookies` kwarg.
    """
    host = (host or "").lower()
    out: Dict[str, str] = {}
    for c in cookie_list or []:
        d = (c.get("domain") or "").lstrip(".").lower()
        if not d or not c.get("name"):
            continue
        if host == d or host.endswith("." + d):
            out[c["name"]] = c["value"]
    return out


def _build_upstream_headers(
    sess: StreamSession, client_headers: Dict[str, str], for_playlist: bool
) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    for k, v in (sess.headers or {}).items():
        if k and v:
            headers[k] = v
    headers["User-Agent"] = sess.user_agent or DEFAULT_UA
    if for_playlist:
        # We must regex-rewrite playlists, so force uncompressed.
        headers["Accept-Encoding"] = "identity"
    # Forward Range (fMP4 seek, Chromecast probe) to upstream.
    for h in _FORWARD_REQ_HEADERS:
        v = client_headers.get(h) or client_headers.get(h.title())
        if v:
            headers["Range"] = v
    return headers


def _looks_like_playlist(target_url: str, content_type: str) -> bool:
    ct = (content_type or "").lower()
    if any(sig in ct for sig in _PLAYLIST_CONTENT_TYPES):
        return True
    low = target_url.lower()
    if low.endswith(".m3u8") or low.endswith(".mpd"):
        return True
    if ".urlset/" in low:
        # Smashystream-family playlists often have .txt extensions.
        # Body-sniff in _proxy_fetch filters out segments that share the
        # .urlset/ path prefix (see _is_playlist_body).
        return True
    return False


# Playlist magic bytes — HLS manifests start with #EXTM3U, DASH MPD with
# <MPD or <?xml. Anything else is treated as a media segment even if the
# URL heuristic said "maybe playlist".
def _is_playlist_body(head: bytes) -> bool:
    head = head.lstrip(b"\xef\xbb\xbf")  # strip UTF-8 BOM
    return head.startswith((b"#EXTM3U", b"<MPD", b"<?xml"))


# Chromecast's Default Media Receiver will refuse a response whose Content-Type
# is clearly non-video (font/*, image/*, application/octet-stream). Some origins
# serve HLS segments under disguised extensions (.woff2, .image) with the
# matching bogus content-type to dodge filters. When we recognise this pattern,
# substitute a sensible video content-type so the receiver will decode it.
_BAD_SEG_CT_PREFIXES = ("font/", "image/", "application/font-", "application/octet-stream")


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_HEADER_SCAN_LIMIT = 4096
_TS_PACKET_SIZE = 188


def detect_png_ts_wrapper_offset(head: bytes) -> int:
    """Detect the anti-adblocker 'PNG-wrapped MPEG-TS' pattern.

    Some pirate streaming CDNs (TikTok ad-CDN under masukestin/hanerix/audinifer
    et al., observed 2026-05-18) prefix MPEG-TS segments with a ~62-byte fake
    PNG header. The Chromecast tolerantly skips ahead to the TS sync byte and
    plays the stream; ffmpeg's strict TS demuxer sees the PNG magic and bails
    with 'Invalid data found when processing input'.

    Returns the byte offset where the MPEG-TS stream begins inside `head`, or
    0 if no wrapper is detected (caller serves bytes unchanged). The check is
    deliberately strict: must be a valid PNG signature, must contain IEND, AND
    the byte right after the IEND chunk's CRC must be the TS sync byte 0x47,
    AND the next-expected TS sync byte (188 bytes later) must also be 0x47.
    Three independent signals — vanishingly low false-positive rate.
    """
    if not head.startswith(_PNG_SIGNATURE):
        return 0
    iend = head.find(b"IEND", 0, _PNG_HEADER_SCAN_LIMIT)
    if iend < 0:
        return 0
    # PNG IEND chunk: 4-byte type ('IEND') + 4-byte CRC. TS data follows.
    ts_start = iend + 4 + 4
    if ts_start >= len(head):
        return 0
    if head[ts_start] != 0x47:
        return 0
    second_sync = ts_start + _TS_PACKET_SIZE
    if second_sync < len(head) and head[second_sync] != 0x47:
        return 0
    return ts_start


def _infer_segment_content_type(target_url: str, upstream_ct: str) -> str:
    """Return the Content-Type to send the Chromecast for a media segment.

    If upstream's content-type is sensible (video/* / audio/*), pass it through.
    Otherwise infer from the URL extension and default to video/mp2t inside a
    .urlset/ HLS context (which is always MPEG-TS for v3 playlists).
    """
    upstream_ct = (upstream_ct or "").split(";")[0].strip().lower()
    if upstream_ct and not upstream_ct.startswith(_BAD_SEG_CT_PREFIXES):
        return upstream_ct
    low = target_url.lower().split("?", 1)[0]
    if low.endswith(".ts"):
        return "video/mp2t"
    if low.endswith(".m4s"):
        return "video/iso.segment"
    if low.endswith(".mp4"):
        return "video/mp4"
    if low.endswith(".webm"):
        return "video/webm"
    if low.endswith(".aac"):
        return "audio/aac"
    # .urlset/ HLS v3 segments are MPEG-TS regardless of the disguised extension.
    if ".urlset/" in low:
        return "video/mp2t"
    return upstream_ct or "application/octet-stream"


async def _proxy_fetch(
    request: web.Request, sess: StreamSession, target_url: str
) -> web.StreamResponse:
    client: ClientSession = request.app["http_client"]
    parsed = urlparse(target_url)
    cookies = _cookies_for_host(sess.cookies, parsed.hostname or "")
    # First attempt: assume non-playlist and send Range if present. If the CDN
    # is a playlist, we'll detect via Content-Type and switch to buffered+rewrite.
    upstream_headers = _build_upstream_headers(sess, dict(request.headers), for_playlist=False)
    log.info(
        "proxy fetch: url=%s cookies=%d range=%r",
        target_url[:140],
        len(cookies),
        upstream_headers.get("Range"),
    )
    # yarl.URL(encoded=True) disables aiohttp's query-string normalization,
    # which otherwise decodes %2F → '/' and breaks signed-URL CDN checks
    # (e.g. tiktok ad segments with x-signature=...%2F...).
    try:
        resp = await client.get(
            yarl.URL(target_url, encoded=True),
            cookies=cookies,
            headers=upstream_headers,
            allow_redirects=True,
            timeout=ClientTimeout(total=60),
        )
    except Exception as e:
        log.exception("upstream fetch failed for %s", target_url[:140])
        return web.Response(status=502, text=f"upstream fetch failed: {e}")

    try:
        ct = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if _looks_like_playlist(target_url, ct):
            # Buffer and sniff — the .urlset/ URL heuristic also matches media
            # segments that live under the same path, and decoding their bytes
            # as text then running rewrite_playlist crashes urljoin on the
            # binary noise (saw "Invalid IPv6 URL" in the wild).
            body_bytes = await resp.read()
            if _is_playlist_body(body_bytes[:16]):
                body = body_bytes.decode("utf-8", errors="replace")
                # P2.4: use request.host (carries both host + port of the actual
                # incoming request) instead of hardcoded lan_ip:PROXY_PORT. In
                # production this is identical (host="<lan_ip>:38123") but in
                # tests/under different binds it correctly reflects the real port.
                proxy_base = f"http://{request.host}"
                rewritten = rewrite_playlist(body, target_url, sess.token, proxy_base)
                out_ct = ct or "application/vnd.apple.mpegurl"
                preview = "\\n".join(rewritten.splitlines()[:10])
                log.info(
                    "proxy: rewrote playlist (%d -> %d bytes) ct=%s status=%s preview=%s",
                    len(body),
                    len(rewritten),
                    out_ct,
                    resp.status,
                    preview[:400],
                )
                headers = {
                    "Content-Type": f"{out_ct}; charset=utf-8",
                    "Cache-Control": "no-store",
                    **_CORS_HEADERS,
                }
                return web.Response(
                    body=rewritten.encode("utf-8"),
                    status=resp.status,
                    headers=headers,
                )
            # URL said playlist but body is binary — fall through to serving
            # the buffered bytes as a segment (with content-type correction).
            seg_ct = _infer_segment_content_type(target_url, ct)
            log.info(
                "proxy: urlset segment served as %s (upstream ct=%s, %d bytes, url=%s)",
                seg_ct,
                ct or "?",
                len(body_bytes),
                target_url[:120],
            )
            headers: Dict[str, str] = {}
            for name in _FORWARD_RESP_HEADERS:
                if name.lower() == "content-type":
                    continue
                v = resp.headers.get(name)
                if v:
                    headers[name.title()] = v
            headers["Content-Type"] = seg_ct
            headers.setdefault("Accept-Ranges", "bytes")
            for k, v in _CORS_HEADERS.items():
                headers[k] = v
            return web.Response(body=body_bytes, status=resp.status, headers=headers)

        # Media bytes — peek first 8KB to detect the PNG-wrapped MPEG-TS
        # anti-adblocker pattern, then stream through (with the wrapper
        # stripped if present).
        peek = await resp.content.read(8192)
        ts_offset = detect_png_ts_wrapper_offset(peek) if peek else 0
        stripped = ts_offset > 0

        out = web.StreamResponse(status=200 if stripped else resp.status)
        for name in _FORWARD_RESP_HEADERS:
            if name.lower() == "content-type":
                continue
            # When we strip a PNG wrapper the served byte-count + range
            # semantics change; let aiohttp use chunked transfer instead of
            # forwarding the now-wrong upstream length/range.
            if stripped and name.lower() in ("content-length", "content-range"):
                continue
            v = resp.headers.get(name)
            if v:
                out.headers[name.title()] = v
        if stripped:
            out.headers["Content-Type"] = "video/mp2t"
        else:
            out.headers["Content-Type"] = _infer_segment_content_type(target_url, ct)
        out.headers.setdefault("Accept-Ranges", "bytes")
        for k, v in _CORS_HEADERS.items():
            out.headers[k] = v
        if stripped:
            log.info(
                "proxy: stripped %d-byte PNG wrapper, serving as video/mp2t (url=%s)",
                ts_offset, target_url[:120],
            )
        log.info(
            "proxy: streaming %s status=%s upstream_ct=%s served_ct=%s",
            target_url[:120],
            resp.status,
            resp.headers.get("Content-Type", "?"),
            out.headers["Content-Type"],
        )
        await out.prepare(request)
        first_payload = peek[ts_offset:] if stripped else peek
        if first_payload:
            await out.write(first_payload)
        async for chunk in resp.content.iter_chunked(64 * 1024):
            await out.write(chunk)
        await out.write_eof()
        return out
    finally:
        resp.release()


async def _handle_options(request: web.Request) -> web.Response:
    """Chromecast's sandbox may issue CORS preflights for our proxy URLs.
    Answer them without forwarding anywhere.
    """
    return web.Response(status=204, headers=_CORS_HEADERS)


async def _handle_entry(request: web.Request) -> web.StreamResponse:
    """Entry route for a registered session — hits the upstream URL that was
    originally registered. Path suffix (master.m3u8 / video.mp4 / ...) is
    cosmetic; the real behavior is content-type driven.
    """
    token = request.match_info["token"]
    sess = request.app["session_store"].get(token)
    if not sess:
        return web.Response(status=404, text="unknown or expired token")
    return await _proxy_fetch(request, sess, sess.upstream_url)


async def _handle_fetch(request: web.Request) -> web.StreamResponse:
    """Nested fetch — the `u` query param is the base64url-encoded upstream URL
    the playlist originally pointed at. ffmpeg hits this for every segment
    and every nested variant playlist (post-P2.4; pre-P2.4 it was Chromecast)."""
    token = request.match_info["token"]
    sess = request.app["session_store"].get(token)
    if not sess:
        return web.Response(status=404, text="unknown or expired token")
    u = request.query.get("u")
    if not u:
        return web.Response(status=400, text="missing u")
    try:
        target_url = decode_url(u)
    except Exception:
        return web.Response(status=400, text="bad u")
    return await _proxy_fetch(request, sess, target_url)


def _transcoder_alive(sess: StreamSession) -> bool:
    """True iff the session's transcoder is in a state where /output/* can serve.
    Defined in one place so the master + segment handlers can't drift."""
    t = sess.transcoder
    if t is None:
        return False
    return t.state in _TRANSCODER_ALIVE_STATES


def _503_for_transcoder(sess: StreamSession) -> web.Response:
    """Build the standard 503 body for /output/* when the transcoder isn't alive."""
    t = sess.transcoder
    body: Dict[str, Any] = {
        "state": t.state.value if t is not None else "none",
        "idle_reason": t.idle_reason if t is not None else None,
    }
    return web.json_response(body, status=503, headers=_CORS_HEADERS)


async def _handle_output_manifest(request: web.Request) -> web.StreamResponse:
    """Serve transcoder.output_dir / (master.m3u8 | variant.m3u8) from disk.

    The output_dir property tracks the CURRENT slot — during RELOADING it's
    still OLD; flips to NEW atomically on promotion. So we always serve the
    file that's actually being written to.
    """
    token = request.match_info["token"]
    sess = request.app["session_store"].get(token)
    if sess is None:
        return web.Response(status=404, text="unknown or expired token")
    if not _transcoder_alive(sess):
        return _503_for_transcoder(sess)
    # Pick master.m3u8 vs variant.m3u8 from the path suffix.
    filename = request.path.rsplit("/", 1)[-1]   # e.g. "master.m3u8"
    if filename not in ("master.m3u8", "variant.m3u8"):
        return web.Response(status=404, text="unknown output manifest")
    path = sess.transcoder.output_dir / filename
    if not path.is_file():
        # Race: transcoder JUST flipped from RELOADING to STREAMING and the
        # OLD slot's master.m3u8 was rmtree'd a millisecond ago. Treat as
        # "not yet" — 503; client will retry.
        return _503_for_transcoder(sess)
    body = path.read_bytes()
    headers = {
        "Content-Type": "application/vnd.apple.mpegurl; charset=utf-8",
        "Cache-Control": "no-store",
        **_CORS_HEADERS,
    }
    return web.Response(body=body, status=200, headers=headers)


async def _handle_output_segment(request: web.Request) -> web.StreamResponse:
    """Serve transcoder.output_dir / seg_NNNNN.ts from disk."""
    token = request.match_info["token"]
    sess = request.app["session_store"].get(token)
    if sess is None:
        return web.Response(status=404, text="unknown or expired token")
    if not _transcoder_alive(sess):
        return _503_for_transcoder(sess)
    n = request.match_info["n"]            # 5-digit numeric per route regex
    path = sess.transcoder.output_dir / f"seg_{n}.ts"
    if not path.is_file():
        return web.Response(status=404, text="segment not found")
    body = path.read_bytes()
    headers = {
        "Content-Type": "video/mp2t",
        "Accept-Ranges": "bytes",
        **_CORS_HEADERS,
    }
    return web.Response(body=body, status=200, headers=headers)


async def _nm(request: web.Request) -> web.Response:
    try:
        msg = await request.json()
    except Exception as e:
        return web.json_response(
            {"type": "error", "detail": f"invalid JSON: {e}"}, status=400
        )
    if not isinstance(msg, dict) or "type" not in msg:
        return web.json_response(
            {"type": "error", "detail": "missing 'type'"}, status=400
        )
    handlers: Dict[str, NMHandler] = request.app["nm_handlers"]
    handler = handlers.get(msg["type"])
    if handler is None:
        return web.json_response(
            {"type": "error", "detail": f"unknown type '{msg['type']}'"}
        )
    try:
        resp = await handler(request.app, msg)
    except Exception as e:
        log.exception("nm handler for %s crashed", msg["type"])
        return web.json_response({"type": "error", "detail": str(e)})
    return web.json_response(resp)


async def _on_startup(app: web.Application) -> None:
    # P2.6: wipe leftover per-session output dirs from prior crashed/killed
    # runs. main._another_instance_healthy already gated duplicates, so we
    # own this machine's castbooster state by the time we reach here.
    sweep_result = sweep_stranded_output_dirs()
    log.info(
        "startup sweep: swept=%d errors=%d",
        sweep_result["swept"], sweep_result["errors"],
    )
    # ClientSession must be created inside the loop that will use it.
    app["http_client"] = ClientSession()
    cm = CastManager()
    cm.start()
    # Hand the proxy thread's event loop to the manager so it can spawn
    # WiFi-radio keepalive tasks from caster.py executor threads.
    cm.attach_loop(asyncio.get_running_loop())
    app["cast_manager"] = cm
    # P2.4: cache AccelProfile so _handle_cast doesn't re-probe per cast.
    # Probe failures are non-fatal — the passthrough path is still viable.
    try:
        app["accel_profile"] = ffmpeg_probe.detect()
        log.info(
            "ffmpeg probe ok: tier=%s encoder=%s decoder=%s path=%s",
            app["accel_profile"].tier,
            app["accel_profile"].encoder,
            app["accel_profile"].decoder,
            app["accel_profile"].ffmpeg_path,
        )
    except (FFmpegNotFoundError, FFmpegProbeError) as e:
        log.warning(
            "ffmpeg probe failed (%s); every cast will use passthrough", e,
        )
        app["accel_profile"] = None

    # P2.4: log if the dev escape hatch is engaged at startup.
    if _is_passthrough_env_set():
        log.warning(
            "CASTBOOSTER_PASSTHROUGH=1 — transcoder bypassed; every cast is passthrough"
        )


async def _on_cleanup(app: web.Application) -> None:
    client: Optional[ClientSession] = app.get("http_client")
    if client is not None:
        await client.close()
    cm: Optional[CastManager] = app.get("cast_manager")
    if cm is not None:
        # Discovery stop can block briefly on socket close; run off-loop.
        await asyncio.get_running_loop().run_in_executor(None, cm.stop)


def _build_app(lan_ip: str) -> web.Application:
    app = web.Application()
    app["lan_ip"] = lan_ip
    app["session_store"] = SessionStore()
    app["nm_handlers"] = _default_handlers()
    app.router.add_get("/health", _health)
    app.router.add_post("/nm", _nm)
    # Entry routes for registered streams. Path suffix is cosmetic — Chromecast
    # uses it as a hint, we serve whatever the session's upstream really is.
    # (aiohttp's add_get already auto-handles HEAD, so we only register OPTIONS
    # explicitly for CORS preflight.)
    # P2.4: /s/{token}/upstream/* — session-aware passthrough to the upstream
    # streaming site. Loopback-only (ffmpeg is the legitimate client). The
    # Chromecast does NOT reach here directly post-P2.4 — it reaches /output/*
    # which is served by the transcoder.
    for path in ("/s/{token}/upstream/master.m3u8",
                 "/s/{token}/upstream/manifest.mpd",
                 "/s/{token}/upstream/video.mp4",
                 "/s/{token}/upstream/video.webm",
                 "/s/{token}/upstream/video"):
        app.router.add_get(path, _handle_entry)
        app.router.add_route("OPTIONS", path, _handle_options)
    app.router.add_get("/s/{token}/upstream/fetch.ts", _handle_fetch)
    app.router.add_route("OPTIONS", "/s/{token}/upstream/fetch.ts", _handle_options)
    # P2.4: /s/{token}/output/* — transcoder's local HLS, served from disk.
    # These are LAN-accessible (Chromecast is the legitimate client).
    app.router.add_get("/s/{token}/output/master.m3u8", _handle_output_manifest)
    app.router.add_get("/s/{token}/output/variant.m3u8", _handle_output_manifest)
    app.router.add_get(r"/s/{token}/output/seg_{n:\d{5}}.ts", _handle_output_segment)
    app.router.add_route("OPTIONS", "/s/{token}/output/master.m3u8", _handle_options)
    app.router.add_route("OPTIONS", "/s/{token}/output/variant.m3u8", _handle_options)
    app.router.add_route("OPTIONS", r"/s/{token}/output/seg_{n:\d{5}}.ts", _handle_options)
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    return app


async def _run_server(
    lan_ip: str,
    shutdown: asyncio.Event,
    on_bound: Callable[[], None],
) -> web.Application:
    app = _build_app(lan_ip)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, PROXY_HOST, PROXY_PORT)
    try:
        await site.start()
    except OSError as e:
        log.error("proxy bind failed on %s:%d — %s", PROXY_HOST, PROXY_PORT, e)
        raise
    log.info("proxy listening on %s:%d (LAN %s)", PROXY_HOST, PROXY_PORT, lan_ip)
    on_bound()
    try:
        await shutdown.wait()
    finally:
        log.info("proxy shutting down")
        await runner.cleanup()
    return app


def start_proxy(on_ready: Optional[Callable[[str], None]] = None) -> ProxyHandle:
    handle = ProxyHandle()

    def _thread() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        handle.loop = loop
        handle._shutdown = asyncio.Event()
        handle.lan_ip = get_lan_ip()
        if on_ready:
            on_ready(handle.lan_ip)
        try:
            loop.run_until_complete(
                _run_server(handle.lan_ip, handle._shutdown, handle._ready.set)
            )
        except Exception:
            log.exception("proxy thread crashed")
            handle.bind_failed = True
            handle._ready.set()
        finally:
            loop.close()

    t = threading.Thread(target=_thread, name="castbooster-proxy", daemon=True)
    t.start()
    handle.thread = t
    return handle
