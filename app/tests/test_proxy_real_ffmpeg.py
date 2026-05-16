"""Gated integration test: a real Transcoder against the proxy's /upstream/*,
validating that ffmpeg's HLS demuxer can fetch through _proxy_fetch.

Gated on RUN_REAL_FFMPEG=1 because it spawns a real ffmpeg subprocess.
Wall budget ~25s.
"""
import asyncio
import os
import subprocess
import time
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from castbooster.ffmpeg_probe import locate_ffmpeg, detect
from castbooster.filter_chain import FilterChain, NoopFilter
from castbooster.proxy import _build_app
from castbooster.transcoder import Transcoder, TranscoderState


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_REAL_FFMPEG", "0") != "1",
    reason="set RUN_REAL_FFMPEG=1 to run real-ffmpeg integration tests",
)


def _build_upstream_hls(ffmpeg_path: str, out_dir: Path) -> Path:
    """Generate a tiny 30s HLS from lavfi (no network needed)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    master = out_dir / "master.m3u8"
    subprocess.run(
        [
            ffmpeg_path, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=320x240:rate=24",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-t", "30",
            "-f", "hls", "-hls_time", "2", "-hls_list_size", "0",
            "-hls_segment_filename", str(out_dir / "seg_%05d.ts"),
            str(master),
        ],
        check=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    return master


def test_real_transcoder_through_proxy_reaches_ready_and_serves_master(tmp_path):
    """Spin up the proxy + register a session whose upstream_url points at a
    local HTTP file server serving the lavfi-generated HLS. Spawn a real
    Transcoder against the proxy's /upstream/master.m3u8. Wait READY. Fetch
    /output/master.m3u8 and assert #EXTM3U."""
    async def _go():
        accel = detect()
        ffmpeg_path = accel.ffmpeg_path

        # 1) Generate the upstream HLS we'll pretend is the streaming site.
        src_dir = tmp_path / "src"
        _build_upstream_hls(ffmpeg_path, src_dir)

        # 2) Boot a tiny HTTP server serving src_dir (this is our "upstream site").
        upstream_app = web.Application()
        async def _serve(request):
            rel = request.match_info["tail"]
            path = src_dir / rel
            if not path.is_file():
                return web.Response(status=404)
            return web.FileResponse(path)
        upstream_app.router.add_get("/{tail:.+}", _serve)
        upstream_server = TestServer(upstream_app)
        await upstream_server.start_server()
        upstream_base = f"http://127.0.0.1:{upstream_server.port}"

        # 3) Boot the real castbooster proxy.
        proxy_app = _build_app("127.0.0.1")
        proxy_server = TestServer(proxy_app)
        await proxy_server.start_server()
        proxy_port = proxy_server.port

        try:
            # 4) Register a session pointing at the upstream HLS.
            sess = proxy_app["session_store"].create(
                f"{upstream_base}/master.m3u8",
            )

            # 5) Spawn a REAL Transcoder against the proxy's /upstream/master.m3u8.
            base = tmp_path / "transcoder_out"
            warming_timeout = 12.0    # be generous — sw cold start
            transcoder = Transcoder(
                input_url=f"http://127.0.0.1:{proxy_port}/s/{sess.token}/upstream/master.m3u8",
                base_output_dir=base,
                accel=accel,
                filter_chain=FilterChain([NoopFilter()]),
                warming_timeout=warming_timeout,
                stall_timeout=warming_timeout,
            )
            sess.transcoder = transcoder
            sess.output_dir = base
            transcoder.start()
            try:
                # wait_until_ready is a blocking threading.Event.wait() call.
                # We MUST run it in an executor so the asyncio event loop stays
                # alive to serve the proxy — otherwise the event loop is frozen
                # while ffmpeg is trying to fetch /upstream/master.m3u8 through
                # the proxy, and the transcoder never produces segments.
                loop = asyncio.get_running_loop()
                ready = await loop.run_in_executor(
                    None, transcoder.wait_until_ready, warming_timeout
                )
                assert ready, f"transcoder did not reach READY: state={transcoder.state} idle_reason={transcoder.idle_reason}"

                # 6) NOW fetch /output/master.m3u8 from the proxy as a client.
                import aiohttp
                async with aiohttp.ClientSession() as sess_http:
                    async with sess_http.get(
                        f"http://127.0.0.1:{proxy_port}/s/{sess.token}/output/master.m3u8"
                    ) as resp:
                        assert resp.status == 200, await resp.text()
                        body = await resp.text()
                        assert body.startswith("#EXTM3U"), f"unexpected body: {body[:200]}"
            finally:
                transcoder.stop()
        finally:
            await proxy_server.close()
            await upstream_server.close()

    asyncio.run(_go())
