"""Confirms _on_startup caches AccelProfile on the app dict and survives
ffmpeg_probe.detect() failures (passthrough fallback).

Note: pytest-asyncio is not installed in this project. We follow the
established pattern from test_keepalive.py: drive async code with
asyncio.run() inside ordinary pytest functions.
"""
import asyncio
from unittest.mock import patch

from aiohttp import web

from castbooster.proxy import _build_app
from castbooster.ffmpeg_probe import (
    AccelProfile, FFmpegNotFoundError, FFmpegProbeError,
)


def test_on_startup_caches_accel_profile_on_app():
    fake_profile = AccelProfile(
        ffmpeg_path="C:/fake/ffmpeg.exe",
        encoder="h264_nvenc", decoder="cuda", tier="nvidia",
    )

    async def _go():
        with patch("castbooster.proxy.ffmpeg_probe.detect", return_value=fake_profile):
            app = _build_app("192.168.1.10")
            runner = web.AppRunner(app)
            await runner.setup()
            try:
                assert app["accel_profile"] is fake_profile
            finally:
                await runner.cleanup()

    asyncio.run(_go())


def test_on_startup_handles_ffmpeg_not_found():
    async def _go():
        with patch("castbooster.proxy.ffmpeg_probe.detect",
                   side_effect=FFmpegNotFoundError("no ffmpeg")):
            app = _build_app("192.168.1.10")
            runner = web.AppRunner(app)
            await runner.setup()
            try:
                assert app["accel_profile"] is None
            finally:
                await runner.cleanup()

    asyncio.run(_go())


def test_on_startup_handles_ffmpeg_probe_error():
    async def _go():
        with patch("castbooster.proxy.ffmpeg_probe.detect",
                   side_effect=FFmpegProbeError("every encoder failed")):
            app = _build_app("192.168.1.10")
            runner = web.AppRunner(app)
            await runner.setup()
            try:
                assert app["accel_profile"] is None
            finally:
                await runner.cleanup()

    asyncio.run(_go())
