"""End-to-end orchestrator test against a fake cloud worker.

Verifies the orchestrator + v0.3.0 contract + Bearer auth + playlist_ready
polling all wire together -- no GPU, no real RunPod, no real ffmpeg.

The orchestrator runs against a real uvicorn-hosted FastAPI fake worker on
localhost. RunPodClient is mocked so we never touch the RunPod API; the mock
returns a PodInfo whose public_url points at the fake worker. The fake worker
implements just enough of the v0.3.0 contract -- /healthz unauthenticated,
/process + /process_status Bearer-gated -- for the orchestrator to walk its
full happy path AND its failure paths (playlist-ready timeout, 409
already-running).

Each test uses a distinct port (18081-18085) so a slow OS-level socket TIME_WAIT
on one port can't flake the next test.
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from app.castbooster.cloud.orchestrator import (
    CloudOrchestrator,
    OrchestrationResult,
    OrchestratorError,
)
from app.castbooster.cloud.runpod_client import PodInfo
from app.castbooster.cloud.state import CloudState

from .fake_cloud_worker import (
    FakeWorkerState,
    run_fake_worker,
)


def _make_orchestrator(tmp_path, public_url):
    """Build a real orchestrator with a MagicMock RunPodClient that returns
    the fake worker's URL on create_pod."""
    rp = MagicMock()
    rp.create_pod.return_value = PodInfo(
        pod_id="fakepod",
        public_url=public_url,
        gpu_type="fake-gpu",
    )
    rp.terminate.return_value = None
    state = CloudState(path=tmp_path / "state.json")
    orch = CloudOrchestrator(
        runpod=rp,
        state=state,
        cloud_api_key="fake-cloud-api-key",
        image="ghcr.io/fake/worker:v0.3.1",
        gpu_types=["fake-gpu"],
        runpod_api_key="fake-rp-key",
        healthz_boot_timeout_s=5.0,
        healthz_poll_interval_s=0.1,
        playlist_ready_timeout_s=5.0,
        playlist_ready_poll_interval_s=0.1,
        sleep_fn=time.sleep,  # real sleeps -- but small intervals
    )
    return orch, rp


def test_e2e_orchestrator_against_fake_worker_returns_hls_url_when_ready(tmp_path):
    fake_state = FakeWorkerState(playlist_ready_after_s=0.3)  # ready after 300ms
    with run_fake_worker(fake_state, port=18081) as (base_url, _):
        orch, rp = _make_orchestrator(tmp_path, base_url)
        try:
            result = orch.ensure_pod_and_process(
                source_url="https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8",
                source_headers={"Cookie": "session=abc"},
            )
        finally:
            orch.close()
    assert isinstance(result, OrchestrationResult)
    assert result.hls_url == "https://fake-pod-8080.proxy.runpod.net/hls/playlist.m3u8"
    assert result.pod_id == "fakepod"
    rp.create_pod.assert_called_once()


def test_e2e_orchestrator_passes_bearer_auth_and_source_headers(tmp_path):
    fake_state = FakeWorkerState(playlist_ready_after_s=0.2)
    with run_fake_worker(fake_state, port=18082) as (base_url, state):
        orch, _ = _make_orchestrator(tmp_path, base_url)
        try:
            orch.ensure_pod_and_process(
                source_url="https://egydead.example/anime.m3u8",
                source_headers={
                    "Cookie": "session=abc; auth=xyz",
                    "User-Agent": "TestUA/1.0",
                },
            )
        finally:
            orch.close()
    # All authenticated calls (POST /process + >=1 GET /process_status) must
    # have used the correct bearer token.
    bearers = [h for h in state.auth_headers_seen if h]
    assert bearers, "expected at least one auth-gated request"
    assert all(h == "Bearer fake-cloud-api-key" for h in bearers), bearers
    # The /process body must have passed source_headers through verbatim.
    assert state.last_process_body is not None
    assert state.last_process_body["source_url"] == "https://egydead.example/anime.m3u8"
    assert state.last_process_body["source_headers"]["Cookie"] == "session=abc; auth=xyz"
    assert state.last_process_body["source_headers"]["User-Agent"] == "TestUA/1.0"


def test_e2e_orchestrator_raises_on_playlist_ready_timeout(tmp_path):
    fake_state = FakeWorkerState(playlist_ready_after_s=60.0)  # never ready in test budget
    with run_fake_worker(fake_state, port=18083) as (base_url, _):
        orch, rp = _make_orchestrator(tmp_path, base_url)
        try:
            # 5s playlist_ready_timeout_s (from _make_orchestrator) -- far short
            # of 60s fake delay; must surface as OrchestratorError.
            with pytest.raises(OrchestratorError, match="never ready"):
                orch.ensure_pod_and_process(source_url="x")
        finally:
            orch.close()
    # Failed pod terminated to avoid leaking a paid-by-minute resource.
    rp.terminate.assert_called_with("fakepod")


def test_e2e_orchestrator_raises_on_409_already_running(tmp_path):
    fake_state = FakeWorkerState(playlist_ready_after_s=0.1, process_returns_409=True)
    with run_fake_worker(fake_state, port=18084) as (base_url, _):
        orch, rp = _make_orchestrator(tmp_path, base_url)
        try:
            with pytest.raises(OrchestratorError, match="already running"):
                orch.ensure_pod_and_process(source_url="x")
        finally:
            orch.close()
    rp.terminate.assert_called_with("fakepod")


def test_e2e_orchestrator_uses_healthcheck_before_calling_process(tmp_path):
    """/healthz must respond 200 before /process is called. Verified by
    observing the auth_headers_seen ordering: /healthz is unauthenticated and
    deliberately does NOT append to auth_headers_seen, so the only entries are
    /process + /process_status. If those entries exist at all, /process
    succeeded -- meaning the prior unauthenticated /healthz returned 200
    (otherwise _wait_for_healthy would have raised OrchestratorError before
    even attempting /process)."""
    fake_state = FakeWorkerState(playlist_ready_after_s=0.2)
    with run_fake_worker(fake_state, port=18085) as (base_url, state):
        orch, _ = _make_orchestrator(tmp_path, base_url)
        try:
            orch.ensure_pod_and_process(source_url="https://test.m3u8")
        finally:
            orch.close()
    # The fake records auth on /process + /process_status (not /healthz).
    # Should have >=2 entries (1 for /process, at least 1 for /process_status).
    assert len(state.auth_headers_seen) >= 2, (
        f"expected at least 2 authenticated calls, got {len(state.auth_headers_seen)}"
    )
