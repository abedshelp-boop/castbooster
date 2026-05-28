"""Minimal fake of the v0.3.0 cloud worker for integration tests.

Mimics /healthz + /process (returns async shape immediately) + /process_status
(toggleable playlist_ready) without real ffmpeg/RIFE/GPU. Spun up in a
background thread per-test via run_fake_worker().

Auth: matches v0.3.0 -- Bearer CLOUD_API_KEY on /process and /process_status.
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator, Optional

import httpx
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse


@dataclass
class FakeWorkerState:
    """Per-instance state knobs the test controls between assertions."""
    api_key: str = "fake-cloud-api-key"
    # When does playlist_ready flip to True relative to /process call?
    playlist_ready_after_s: float = 0.0
    # If True, /process returns 409 (already running).
    process_returns_409: bool = False
    # If non-None, /process returns this status code with this JSON body.
    process_override_response: Optional[tuple[int, dict]] = None
    # Recording the most recent /process body for assertions.
    last_process_body: Optional[dict] = None
    process_started_at: Optional[float] = None
    # Captures every Authorization header seen on authenticated endpoints,
    # in order, for auth assertions. /healthz is NOT auth-gated so it does
    # NOT append to this list -- that ordering invariant is what
    # test_e2e_orchestrator_uses_healthcheck_before_calling_process relies on.
    auth_headers_seen: list[str] = field(default_factory=list)


def _build_fake_app(state: FakeWorkerState) -> FastAPI:
    app = FastAPI()

    def _require_auth(authorization: Optional[str]):
        state.auth_headers_seen.append(authorization or "")
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "missing bearer")
        if authorization.removeprefix("Bearer ").strip() != state.api_key:
            raise HTTPException(403, "bad bearer")

    @app.get("/healthz")
    async def healthz():
        # NOT auth-gated -- mirrors v0.3.0 worker; orchestrator's _healthcheck
        # calls this without Authorization. Crucially, do NOT touch
        # state.auth_headers_seen here -- the ordering assertion in
        # test_e2e_orchestrator_uses_healthcheck_before_calling_process
        # depends on /healthz being absent from that list.
        return {"status": "ok"}

    @app.post("/process")
    async def process(req: Request, authorization: Optional[str] = Header(default=None)):
        _require_auth(authorization)
        body = await req.json()
        state.last_process_body = body
        if state.process_override_response is not None:
            code, payload = state.process_override_response
            return JSONResponse(status_code=code, content=payload)
        if state.process_returns_409:
            # v0.3.0 contract: 409 with a body that carries the still-running
            # session info. Orchestrator surfaces this as OrchestratorError.
            return JSONResponse(
                status_code=409,
                content={
                    "state": "running",
                    "hls_url": "https://fake-pod-8080.proxy.runpod.net/hls/playlist.m3u8",
                    "source_url": body.get("source_url", ""),
                    "started_at": state.process_started_at or time.time(),
                },
            )
        state.process_started_at = time.time()
        return {
            "hls_url": "https://fake-pod-8080.proxy.runpod.net/hls/playlist.m3u8",
            "state": "running",
            "started_at": state.process_started_at,
            "source_url": body.get("source_url", ""),
        }

    @app.get("/process_status")
    async def process_status(authorization: Optional[str] = Header(default=None)):
        _require_auth(authorization)
        ready = False
        elapsed = None
        if state.process_started_at is not None:
            elapsed = time.time() - state.process_started_at
            ready = elapsed >= state.playlist_ready_after_s
        return {
            "state": "running" if state.process_started_at is not None else "idle",
            "hls_url": "https://fake-pod-8080.proxy.runpod.net/hls/playlist.m3u8",
            "source_url": (state.last_process_body or {}).get("source_url"),
            "started_at": state.process_started_at,
            "elapsed_s": elapsed,
            "playlist_ready": ready,
            "n_segments": 6 if ready else 0,
            "pipeline_returncode": None,
            "error_type": None,
            "error_msg": None,
            "stderr_tail": None,
            "traceback": None,
        }

    return app


def _wait_for_healthz(base_url: str, deadline: float) -> bool:
    """Poll /healthz until it returns 200 OR deadline expires."""
    while time.time() < deadline:
        try:
            r = httpx.get(f"{base_url}/healthz", timeout=0.5)
            if r.status_code == 200:
                return True
        except httpx.RequestError:
            pass
        time.sleep(0.05)
    return False


@contextmanager
def run_fake_worker(
    state: Optional[FakeWorkerState] = None,
    port: int = 18080,
) -> Iterator[tuple[str, FakeWorkerState]]:
    """Spin up the fake worker on localhost:port for the duration of the with-block.

    Yields (base_url, state) so the test can both point the orchestrator at the
    URL AND mutate state to drive scenarios (e.g. flip playlist_ready_after_s).

    Uses uvicorn.Server.started as the primary readiness signal, falling back
    to a /healthz probe -- the probe makes the startup robust across uvicorn
    versions where `started` flips before the socket is actually accepting.
    """
    if state is None:
        state = FakeWorkerState()
    app = _build_fake_app(state)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 10.0
    # Wait for the server's started flag first (uvicorn 0.32+ exposes it),
    # then fall back to a /healthz probe.
    while time.time() < deadline:
        if getattr(server, "started", False):
            break
        time.sleep(0.05)
    if not _wait_for_healthz(base_url, deadline):
        server.should_exit = True
        thread.join(timeout=5)
        raise RuntimeError(f"fake worker did not become healthy on {base_url} within 10s")

    try:
        yield base_url, state
    finally:
        server.should_exit = True
        thread.join(timeout=5)
