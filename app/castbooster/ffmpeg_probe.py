"""Probe ffmpeg capabilities at startup.

Locates the ffmpeg binary, enumerates the hardware-acceleration paths and H.264
encoders the build supports, then runs a tiny 1-frame encode test against each
candidate to pick the first one that ACTUALLY works on this machine. Result is
cached per-process via lru_cache.

The encoder runtime test is the key defense against ffmpeg listing encoders
that compile-time exist but fail at runtime (e.g. listing h264_nvenc on a
machine with no NVIDIA driver, which happens on Abed's Snapdragon dev box).
"""
from __future__ import annotations

# The non-`logging` imports below (os, shutil, subprocess, sys, dataclass, field,
# lru_cache, Path, Optional) are intentionally added up-front for the helpers
# that land in upcoming P2.1 sub-tasks (locate_ffmpeg, try_encoder, detect,
# AccelProfile). Removing them here would just force the next task's diff to
# re-add them — keeping the import block stable across tasks reduces noise.
# noqa: F401 directives are not needed because every name below is referenced
# by the time the file reaches its final P2.1 state.
import json
import logging
import os                                                                # noqa: F401  (used by locate_ffmpeg, Task 6)
import shutil                                                            # noqa: F401  (used by locate_ffmpeg, Task 6)
import subprocess                                                        # noqa: F401  (used by try_encoder + _run, Task 7)
import sys                                                               # noqa: F401  (used by _run, Task 7)
from dataclasses import dataclass, field                                 # noqa: F401  (used by AccelProfile, Task 8)
from functools import lru_cache                                          # noqa: F401  (used by detect, Task 8)
from pathlib import Path                                                 # noqa: F401  (used by locate_ffmpeg, Task 6)
from typing import List, Optional                                        # noqa: F401  (used by locate_ffmpeg, Task 6)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AccelProfile:
    """Immutable summary of ffmpeg capabilities + the chosen encode lane.

    Consumed by the transcoder (P2.2) to build its ffmpeg command line.
    """
    ffmpeg_path: str
    encoder: str            # "h264_nvenc" | "h264_qsv" | "h264_amf" | "libx264"
    decoder: str            # "cuda" | "qsv" | "d3d11va" | "dxva2" | "none"
    tier: str               # "nvidia" | "intel" | "amd" | "sw"
    available_encoders: List[str] = field(default_factory=list)
    available_hwaccels: List[str] = field(default_factory=list)
    ffmpeg_version: str = ""


# Priority order for encoder selection. Earlier entries win when both are usable.
_ENCODER_CANDIDATES = ("h264_nvenc", "h264_qsv", "h264_amf", "libx264")
_TIER_FOR_ENCODER = {
    "h264_nvenc": "nvidia",
    "h264_qsv": "intel",
    "h264_amf": "amd",
    "libx264": "sw",
}
# Decoder priority per tier. "none" means SW decode (always available).
#
# 2026-05-17 amendment: sw tier now defaults to "none" (pure SW decode).
# Hypothesis: when libx264 is the encoder, pairing with `-hwaccel d3d11va`
# triggers an early D3D11 device-context initialization that hangs for the
# full warming budget on some machines, producing no stderr output and
# never reaching the input-URL fetch.  Forcing decoder="none" sidesteps the
# hwaccel path entirely.  See plan
# C:\Users\Abeds\.claude\plans\yo-continuing-p2-4-proxy-modular-sundae.md.
_DECODER_FOR_TIER = {
    "nvidia": ("cuda", "d3d11va", "none"),
    "intel":  ("qsv",  "d3d11va", "none"),
    "amd":    ("d3d11va", "dxva2", "none"),
    "sw":     ("none",),
}


def parse_hwaccels(output: str) -> List[str]:
    """Parse `ffmpeg -hwaccels` stdout into a list of accel method names.

    Format (canonical):
        Hardware acceleration methods:
        cuda
        vaapi
        ...
    First non-blank line ends with ':' — that's the header, skip it.
    """
    out: List[str] = []
    for line in (output or "").splitlines():
        s = line.strip()
        if not s:
            continue
        if s.endswith(":"):
            continue
        out.append(s)
    return out


def parse_encoders(output: str) -> List[str]:
    """Parse `ffmpeg -encoders` stdout into a list of encoder names.

    Format (canonical):
        Encoders:
         V..... = Video
         A..... = Audio
         ...
         ------
         V....D h264_nvenc           NVIDIA NVENC H.264 encoder (codec h264)
         V..... h264_qsv             H.264 / AVC / MPEG-4 AVC ...
         ...
    Each entry line has a 6-char flag block, then the encoder name, then
    a free-form description. We only process lines AFTER the `------`
    divider so the legend rows above it don't poison the result.
    """
    out: List[str] = []
    in_table = False
    for raw in (output or "").splitlines():
        stripped = raw.strip()
        if stripped.startswith("------"):
            in_table = True
            continue
        if not in_table or not stripped:
            continue
        parts = stripped.split(None, 2)
        if len(parts) < 2:
            continue
        flags, name = parts[0], parts[1]
        # Real flag blocks are exactly 6 chars and start with V/A/S.
        if len(flags) != 6:
            continue
        if flags[0] not in ("V", "A", "S"):
            continue
        out.append(name)
    return out


class FFmpegNotFoundError(RuntimeError):
    """No ffmpeg binary could be located via override, env, bundled, or PATH."""


class FFmpegProbeError(RuntimeError):
    """ffmpeg was located but every H.264 encoder probe failed (libx264 included).

    This should be impossible in practice — libx264 has no hardware dependency.
    Seeing this typically means the vendored binary is corrupt or being blocked
    by AV / EDR.
    """


@dataclass(frozen=True)
class InputVideoInfo:
    """Result of probe_input_video() for a single video stream.

    Fields:
        fps: source frame rate as a float, or None if the source is VFR /
             has unknown avg_frame_rate / probe failed to determine it.
             Callers (P3.4 `_handle_cast`) treat None as "skip interpolation,
             cast through with NoopFilter".
        width, height: pixel dimensions of the first video stream.
        pix_fmt: ffmpeg pixel format name, e.g. "yuv420p".
    """
    fps: Optional[float]
    width: int
    height: int
    pix_fmt: str


_VIDEO_PROBE_TIMEOUT = 10.0  # seconds — HLS manifest fetch may take a beat


def _locate_ffprobe(ffmpeg_path: str) -> Optional[str]:
    """Derive the ffprobe binary path from the ffmpeg binary path.

    The bundled Gyan ffmpeg zip ships ffprobe.exe in the same directory.
    Returns None if no sibling ffprobe binary is present (caller treats
    that as a probe failure → passthrough cast).
    """
    p = Path(ffmpeg_path)
    name = "ffprobe.exe" if sys.platform == "win32" else "ffprobe"
    candidate = p.parent / name
    return str(candidate) if candidate.is_file() else None


def _parse_rational_fps(value: str) -> Optional[float]:
    """Parse ffprobe's 'num/den' fraction string into a positive float.

    Returns None on: empty string, missing numerator or denominator,
    non-numeric components, zero denominator (division by zero), zero
    numerator (0 fps is not useful), negative numerator or denominator.

    Examples:
        "24000/1001" → ~23.976
        "30/1"       → 30.0
        "0/0"        → None   (ffprobe's "unknown" sentinel)
        "0/1"        → None   (zero fps not useful)
        ""           → None
        "abc"        → None
    """
    if not value or "/" not in value:
        return None
    num_str, _, den_str = value.partition("/")
    if not num_str or not den_str:
        return None
    try:
        num = int(num_str)
        den = int(den_str)
    except ValueError:
        return None
    if num <= 0 or den <= 0:
        return None
    return num / den


_BUNDLED_FFMPEG = Path(__file__).parent / "bin" / "ffmpeg.exe"


def locate_ffmpeg(override: Optional[str] = None) -> str:
    """Find ffmpeg in priority order: override arg, env, bundled, PATH.

    Returns the absolute path. Raises FFmpegNotFoundError if none of the
    candidates point at a real file.
    """
    candidates: List[Optional[str]] = []
    if override:
        candidates.append(override)
    env = os.environ.get("CASTBOOSTER_FFMPEG")
    if env:
        candidates.append(env)
    candidates.append(str(_BUNDLED_FFMPEG))
    path_lookup = shutil.which("ffmpeg")
    if path_lookup:
        candidates.append(path_lookup)
    for c in candidates:
        if c and Path(c).is_file():
            return str(Path(c).resolve())
    raise FFmpegNotFoundError(
        f"ffmpeg not found. Tried: {candidates}. "
        f"Run app/scripts/fetch_ffmpeg.ps1 to download it."
    )


_PROBE_TIMEOUT = 5.0  # seconds per encoder probe


def _run(args: List[str], *, timeout: float) -> subprocess.CompletedProcess:
    """subprocess.run wrapper that suppresses the flashing console window on
    Windows and always captures both stdout and stderr as text."""
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NO_WINDOW
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=creationflags,
    )


def try_encoder(ffmpeg_path: str, encoder: str) -> bool:
    """Run a tiny encode-then-discard test. Returns True iff ffmpeg exits 0.

    Why we do this instead of trusting `-encoders` enumeration: ffmpeg lists
    encoders that compile-time exist but fail at runtime — e.g. it lists
    h264_nvenc even on a machine with no NVIDIA driver. The only way to know
    an encoder REALLY works is to actually try one frame.
    """
    args = [
        ffmpeg_path, "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=size=64x64:rate=1:duration=0.1",
        "-c:v", encoder, "-t", "0.05",
        "-f", "null", "-",
    ]
    try:
        result = _run(args, timeout=_PROBE_TIMEOUT)
        if result.returncode != 0:
            log.info(
                "encoder %s runtime probe failed (exit=%d): %s",
                encoder, result.returncode, (result.stderr or "")[:200],
            )
            return False
        return True
    except subprocess.TimeoutExpired:
        log.warning("encoder %s timed out after %ss", encoder, _PROBE_TIMEOUT)
        return False
    except Exception:
        log.exception("encoder %s probe crashed", encoder)
        return False


def _pick_decoder(tier: str, available_hwaccels: List[str]) -> str:
    """Pick the best decoder for the given tier, prefer matched HW decode."""
    for candidate in _DECODER_FOR_TIER[tier]:
        if candidate == "none":
            return "none"
        if candidate in available_hwaccels:
            return candidate
    return "none"


def _ffmpeg_version(ffmpeg_path: str) -> str:
    """Return the version token from `ffmpeg -version`'s first line.

    Best-effort — returns "" on any failure, since version is informational
    only and not load-bearing.
    """
    try:
        r = _run([ffmpeg_path, "-hide_banner", "-version"], timeout=_PROBE_TIMEOUT)
        first = (r.stdout or "").splitlines()[0] if r.stdout else ""
        tokens = first.split()
        # "ffmpeg version 8.1-full_build-www.gyan.dev Copyright (c) ..."
        if len(tokens) >= 3 and tokens[0] == "ffmpeg" and tokens[1] == "version":
            return tokens[2]
    except Exception:
        log.exception("ffmpeg -version failed")
    return ""


@lru_cache(maxsize=2)
def detect(ffmpeg_path_override: Optional[str] = None) -> AccelProfile:
    """Locate ffmpeg, enumerate capabilities, pick a working encode lane.

    Cached per-process via lru_cache. Subsequent calls with the same args
    return the previously-computed AccelProfile without running ffmpeg again.

    Raises FFmpegNotFoundError if no ffmpeg binary is found.
    Raises FFmpegProbeError if every encoder candidate fails the runtime test
    (which would mean even libx264 didn't work — vendored binary is corrupt
    or being blocked).
    """
    ffmpeg_path = locate_ffmpeg(ffmpeg_path_override)
    version = _ffmpeg_version(ffmpeg_path)

    hw_proc  = _run([ffmpeg_path, "-hide_banner", "-hwaccels"], timeout=5.0)
    enc_proc = _run([ffmpeg_path, "-hide_banner", "-encoders"], timeout=5.0)
    hwaccels = parse_hwaccels(hw_proc.stdout or "")
    encoders = parse_encoders(enc_proc.stdout or "")

    chosen: Optional[str] = None
    for cand in _ENCODER_CANDIDATES:
        if cand not in encoders:
            continue
        if try_encoder(ffmpeg_path, cand):
            chosen = cand
            break
    if chosen is None:
        raise FFmpegProbeError(
            f"every encoder candidate failed runtime probe (tried "
            f"{list(_ENCODER_CANDIDATES)}). ffmpeg={ffmpeg_path}"
        )

    tier = _TIER_FOR_ENCODER[chosen]
    decoder = _pick_decoder(tier, hwaccels)

    profile = AccelProfile(
        ffmpeg_path=ffmpeg_path,
        encoder=chosen,
        decoder=decoder,
        tier=tier,
        available_encoders=encoders,
        available_hwaccels=hwaccels,
        ffmpeg_version=version,
    )
    log.info(
        "ffmpeg probe: path=%s version=%s tier=%s encoder=%s decoder=%s",
        profile.ffmpeg_path, profile.ffmpeg_version,
        profile.tier, profile.encoder, profile.decoder,
    )
    return profile


def probe_input_video(
    url: str,
    ffmpeg_path: Optional[str] = None,
) -> Optional[InputVideoInfo]:
    """Probe a video URL for fps / dimensions / pix_fmt via ffprobe.

    Runs `ffprobe -hide_banner -loglevel error -show_streams -select_streams v:0
    -of json <url>` and parses the first video stream's metadata.

    Args:
        url: input URL (HLS m3u8, mp4, file://, etc.).
        ffmpeg_path: optional override; defaults to detect().ffmpeg_path.
            (Used in tests; production callers can rely on the default.)

    Returns:
        InputVideoInfo on a successful probe with a video stream present.
        None on any failure mode:
            - ffprobe binary not found next to ffmpeg
            - subprocess timeout, crash, or non-zero exit
            - ffprobe stdout is not valid JSON
            - JSON has an empty 'streams' array (no video stream after
              -select_streams v:0 — happens on audio-only inputs)
            - first stream is missing required fields (width/height/pix_fmt)

    CFR fps semantics:
        fps = parse(r_frame_rate) iff r_frame_rate == avg_frame_rate.
        Otherwise (including avg_frame_rate == "0/0", which is ffprobe's
        unknown-duration sentinel for HLS): fps = None — caller treats as
        VFR and skips interpolation but still allows passthrough cast.
    """
    if ffmpeg_path is None:
        ffmpeg_path = detect().ffmpeg_path
    ffprobe = _locate_ffprobe(ffmpeg_path)
    if ffprobe is None:
        log.warning("ffprobe not found next to %s", ffmpeg_path)
        return None
    args = [
        ffprobe, "-hide_banner", "-loglevel", "error",
        "-show_streams", "-select_streams", "v:0",
        "-of", "json", url,
    ]
    try:
        result = _run(args, timeout=_VIDEO_PROBE_TIMEOUT)
    except subprocess.TimeoutExpired:
        log.warning("ffprobe timed out on %s", url)
        return None
    except Exception:
        log.exception("ffprobe crashed on %s", url)
        return None
    if result.returncode != 0:
        log.warning(
            "ffprobe failed (exit=%d) on %s: %s",
            result.returncode, url, (result.stderr or "")[:200],
        )
        return None
    try:
        data = json.loads(result.stdout or "{}")
    except ValueError:
        log.warning("ffprobe returned malformed JSON on %s", url)
        return None
    streams = data.get("streams") or []
    if not streams:
        log.warning("ffprobe found no video stream on %s", url)
        return None
    s = streams[0]
    try:
        width = int(s["width"])
        height = int(s["height"])
        pix_fmt = str(s["pix_fmt"])
    except (KeyError, ValueError, TypeError):
        log.warning("ffprobe stream missing required fields on %s: %s", url, list(s.keys()))
        return None
    r = s.get("r_frame_rate") or ""
    a = s.get("avg_frame_rate") or ""
    fps = _parse_rational_fps(r) if r and r == a else None
    return InputVideoInfo(fps=fps, width=width, height=height, pix_fmt=pix_fmt)
