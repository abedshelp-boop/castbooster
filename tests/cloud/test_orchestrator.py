"""Tests for app/castbooster/cloud/orchestrator.py (Pillar 3.6 Task 16).

Covers the v0.3.0 async contract:
  - POST /process returns 1s with state=running (not the URL we hand to Chromecast).
  - GET /process_status must be polled until playlist_ready=true.
  - state=failed during polling must fail-fast (don't burn the 90s budget).

Mocks httpx via respx + RunPodClient via MagicMock. Sleeps and now() are
injected so the whole suite runs in milliseconds even when exercising
the playlist-timeout path.
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import httpx
import pytest
import respx

from castbooster.cloud.orchestrator import (
    CloudOrchestrator,
    OrchestrationResult,
    OrchestratorError,
)
from castbooster.cloud.runpod_client import PodInfo, RunPodError
from castbooster.cloud.state import CloudState, PodRecord


# ---- Helpers ----------------------------------------------------------------

def make_now_fn(initial: float = 0.0, step: float = 1.0):
    """Returns a closure that walks the clock forward by `step` on each call.

    Used to drive deadline-based loops deterministically: a `playlist_ready_
    timeout_s=2.0` orchestrator combined with `make_now_fn(0.0, 1.0)` will
    exhaust the deadline after ~2 calls to _now().
    """
    state = [initial]

    def fn():
        v = state[0]
        state[0] += step
        return v
    return fn


def make_status_side_effect(
    *,
    ready_after_calls: int = 1,
    state: str = "running",
    extras: dict | None = None,
):
    """Returns a respx side_effect that flips playlist_ready=true after
    `ready_after_calls` invocations."""
    call_count = [0]
    extras = extras or {}

    def side_effect(request):
        call_count[0] += 1
        ready = call_count[0] >= ready_after_calls
        body = {
            "state": state,
            "hls_url": "https://newpod-8080.proxy.runpod.net/hls/playlist.m3u8",
            "source_url": "https://test.m3u8",
            "started_at": 1000.0,
            "elapsed_s": float(call_count[0]),
            "playlist_ready": ready,
            "n_segments": 3 if ready else 0,
            "pipeline_returncode": None,
            "error_type": None,
            "error_msg": None,
            "stderr_tail": None,
            "traceback": None,
        }
        body.update(extras)
        return httpx.Response(200, json=body)
    return side_effect


# ---- Fixtures ---------------------------------------------------------------

@pytest.fixture
def runpod_mock():
    m = MagicMock()
    m.create_pod.return_value = PodInfo(
        pod_id="newpod",
        public_url="https://newpod-8080.proxy.runpod.net",
        gpu_type="NVIDIA GeForce RTX 4090",
    )
    m.terminate.return_value = None
    return m


# Share one httpx.Client across all tests. Each Client construction is ~550ms
# on Windows because the SSL context loads CA certs from the Windows store.
# 18 tests * 0.55s would be 10s of pure setup. respx intercepts at the
# transport layer so a shared Client works fine with all the @respx.mock
# decorators.
_SHARED_HTTP_CLIENT = httpx.Client()


@pytest.fixture
def orch_factory(tmp_path):
    """Factory so individual tests can override defaults (timeouts, sleep_fn, now_fn)."""
    def factory(runpod, **overrides):
        defaults = dict(
            runpod=runpod,
            state=CloudState(path=tmp_path / "state.json"),
            cloud_api_key="cloud-secret",
            image="ghcr.io/test/worker:v0.3.1",
            gpu_types=["NVIDIA GeForce RTX 4090"],
            runpod_api_key="rp-key",
            healthz_boot_timeout_s=2.0,
            healthz_poll_interval_s=0.01,
            healthz_request_timeout_s=1.0,
            playlist_ready_timeout_s=2.0,
            playlist_ready_poll_interval_s=0.01,
            process_post_timeout_s=1.0,
            sleep_fn=lambda s: None,
            http_client=_SHARED_HTTP_CLIENT,
        )
        defaults.update(overrides)
        return CloudOrchestrator(**defaults)
    return factory


# ---- 1. Init validation -----------------------------------------------------

def test_init_rejects_empty_cloud_api_key(orch_factory, runpod_mock):
    with pytest.raises(ValueError, match="cloud_api_key"):
        orch_factory(runpod_mock, cloud_api_key="")


def test_init_rejects_empty_gpu_types(orch_factory, runpod_mock):
    with pytest.raises(ValueError, match="gpu_types"):
        orch_factory(runpod_mock, gpu_types=[])


# ---- 2. ensure_pod_and_process: happy path ---------------------------------

@respx.mock
def test_ensure_pod_and_process_creates_new_pod_when_state_empty(
    orch_factory, runpod_mock,
):
    respx.get("https://newpod-8080.proxy.runpod.net/healthz").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )
    respx.post("https://newpod-8080.proxy.runpod.net/process").mock(
        return_value=httpx.Response(200, json={
            "hls_url": "https://newpod-8080.proxy.runpod.net/hls/playlist.m3u8",
            "state": "running",
            "started_at": 1000.0,
            "source_url": "https://test.m3u8",
        })
    )
    respx.get("https://newpod-8080.proxy.runpod.net/process_status").mock(
        side_effect=make_status_side_effect(ready_after_calls=1),
    )

    orch = orch_factory(runpod_mock)
    result = orch.ensure_pod_and_process(
        source_url="https://test.m3u8",
        source_headers={"Cookie": "x"},
    )
    assert isinstance(result, OrchestrationResult)
    assert result.hls_url == "https://newpod-8080.proxy.runpod.net/hls/playlist.m3u8"
    assert result.pod_id == "newpod"
    runpod_mock.create_pod.assert_called_once()


# ---- 3. Idle-cache reuse ---------------------------------------------------

@respx.mock
def test_ensure_reuses_cached_pod_within_grace_window(
    orch_factory, runpod_mock, tmp_path,
):
    state = CloudState(path=tmp_path / "state.json")
    now = time.time()
    state.add_pod(PodRecord(
        pod_id="cached",
        public_url="https://cached-8080.proxy.runpod.net",
        created_at=now - 60,
        idle_until=now + 25,  # in-grace
    ))
    state.save()

    respx.get("https://cached-8080.proxy.runpod.net/healthz").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )
    respx.post("https://cached-8080.proxy.runpod.net/process").mock(
        return_value=httpx.Response(200, json={
            "hls_url": "https://cached-8080.proxy.runpod.net/hls/playlist.m3u8",
            "state": "running",
            "started_at": 1000.0,
            "source_url": "https://test.m3u8",
        })
    )
    respx.get("https://cached-8080.proxy.runpod.net/process_status").mock(
        side_effect=make_status_side_effect(ready_after_calls=1),
    )

    orch = orch_factory(runpod_mock, state=state)
    result = orch.ensure_pod_and_process(source_url="https://test.m3u8")
    assert result.pod_id == "cached"
    runpod_mock.create_pod.assert_not_called()


@respx.mock
def test_ensure_does_not_reuse_pod_outside_grace_window_but_still_healthy(
    orch_factory, runpod_mock, tmp_path,
):
    """idle_until is None means no explicit grace -- but a healthy pod is
    still reusable (the grace is an optimization, not a gate)."""
    state = CloudState(path=tmp_path / "state.json")
    state.add_pod(PodRecord(
        pod_id="cached",
        public_url="https://cached-8080.proxy.runpod.net",
        created_at=time.time() - 120,
        idle_until=None,
    ))
    state.save()

    respx.get("https://cached-8080.proxy.runpod.net/healthz").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )
    respx.post("https://cached-8080.proxy.runpod.net/process").mock(
        return_value=httpx.Response(200, json={
            "hls_url": "https://cached-8080.proxy.runpod.net/hls/playlist.m3u8",
            "state": "running",
            "started_at": 1000.0,
            "source_url": "https://test.m3u8",
        })
    )
    respx.get("https://cached-8080.proxy.runpod.net/process_status").mock(
        side_effect=make_status_side_effect(ready_after_calls=1),
    )

    orch = orch_factory(runpod_mock, state=state)
    result = orch.ensure_pod_and_process(source_url="https://test.m3u8")
    assert result.pod_id == "cached"
    runpod_mock.create_pod.assert_not_called()


@respx.mock
def test_ensure_terminates_unhealthy_pod_and_creates_new(
    orch_factory, runpod_mock, tmp_path,
):
    state = CloudState(path=tmp_path / "state.json")
    state.add_pod(PodRecord(
        pod_id="dead",
        public_url="https://dead-8080.proxy.runpod.net",
        created_at=time.time(),
    ))
    state.save()

    respx.get("https://dead-8080.proxy.runpod.net/healthz").mock(
        return_value=httpx.Response(503)
    )
    respx.get("https://newpod-8080.proxy.runpod.net/healthz").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )
    respx.post("https://newpod-8080.proxy.runpod.net/process").mock(
        return_value=httpx.Response(200, json={
            "hls_url": "https://newpod-8080.proxy.runpod.net/hls/playlist.m3u8",
            "state": "running",
            "started_at": 1000.0,
            "source_url": "https://test.m3u8",
        })
    )
    respx.get("https://newpod-8080.proxy.runpod.net/process_status").mock(
        side_effect=make_status_side_effect(ready_after_calls=1),
    )

    orch = orch_factory(runpod_mock, state=state)
    result = orch.ensure_pod_and_process(source_url="https://test.m3u8")
    runpod_mock.terminate.assert_any_call("dead")
    runpod_mock.create_pod.assert_called_once()
    assert result.pod_id == "newpod"


@respx.mock
def test_ensure_clears_idle_until_on_pod_reuse(
    orch_factory, runpod_mock, tmp_path,
):
    """After ensure, the reused pod's idle_until is None so on_session_end's
    terminate isn't blocked by stale grace from a prior cast."""
    state = CloudState(path=tmp_path / "state.json")
    now = time.time()
    state.add_pod(PodRecord(
        pod_id="cached",
        public_url="https://cached-8080.proxy.runpod.net",
        created_at=now - 60,
        idle_until=now + 25,  # future grace
    ))
    state.save()

    respx.get("https://cached-8080.proxy.runpod.net/healthz").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )
    respx.post("https://cached-8080.proxy.runpod.net/process").mock(
        return_value=httpx.Response(200, json={
            "hls_url": "https://cached-8080.proxy.runpod.net/hls/playlist.m3u8",
            "state": "running",
            "started_at": 1000.0,
            "source_url": "https://test.m3u8",
        })
    )
    respx.get("https://cached-8080.proxy.runpod.net/process_status").mock(
        side_effect=make_status_side_effect(ready_after_calls=1),
    )

    orch = orch_factory(runpod_mock, state=state)
    orch.ensure_pod_and_process(source_url="https://test.m3u8")

    pod = state.get_pod("cached")
    assert pod is not None
    assert pod.idle_until is None


# ---- 4. New-pod boot failure -----------------------------------------------

@respx.mock
def test_create_new_pod_raises_when_pod_never_becomes_healthy(
    orch_factory, runpod_mock,
):
    respx.get("https://newpod-8080.proxy.runpod.net/healthz").mock(
        return_value=httpx.Response(503)
    )

    # Drive the deadline via now_fn so the loop exits after a few iterations.
    orch = orch_factory(
        runpod_mock,
        healthz_boot_timeout_s=1.0,
        now_fn=make_now_fn(0.0, 0.5),  # 0, 0.5, 1.0, 1.5 -> exhausts after ~2 ticks
    )
    with pytest.raises(OrchestratorError, match="never became healthy"):
        orch.ensure_pod_and_process(source_url="https://test.m3u8")
    runpod_mock.terminate.assert_any_call("newpod")


# ---- 5. POST /process behaviour --------------------------------------------

@respx.mock
def test_post_process_passes_source_headers_through(
    orch_factory, runpod_mock,
):
    captured = {}

    def process_handler(request):
        captured["body"] = request.read().decode("utf-8")
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={
            "hls_url": "https://newpod-8080.proxy.runpod.net/hls/playlist.m3u8",
            "state": "running",
            "started_at": 1000.0,
            "source_url": "https://test.m3u8",
        })

    respx.get("https://newpod-8080.proxy.runpod.net/healthz").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )
    respx.post("https://newpod-8080.proxy.runpod.net/process").mock(
        side_effect=process_handler
    )
    respx.get("https://newpod-8080.proxy.runpod.net/process_status").mock(
        side_effect=make_status_side_effect(ready_after_calls=1),
    )

    orch = orch_factory(runpod_mock)
    orch.ensure_pod_and_process(
        source_url="https://test.m3u8",
        source_headers={"Cookie": "abc", "User-Agent": "ua"},
    )

    import json
    body = json.loads(captured["body"])
    assert body["source_url"] == "https://test.m3u8"
    assert body["source_headers"] == {"Cookie": "abc", "User-Agent": "ua"}
    assert captured["auth"] == "Bearer cloud-secret"


@respx.mock
def test_post_process_409_raises_orchestrator_error_and_terminates_pod(
    orch_factory, runpod_mock,
):
    respx.get("https://newpod-8080.proxy.runpod.net/healthz").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )
    respx.post("https://newpod-8080.proxy.runpod.net/process").mock(
        return_value=httpx.Response(409, json={
            "state": "running",
            "hls_url": "https://newpod-8080.proxy.runpod.net/hls/playlist.m3u8",
            "source_url": "https://other.m3u8",
            "started_at": 999.0,
        })
    )

    orch = orch_factory(runpod_mock)
    with pytest.raises(OrchestratorError, match="already running"):
        orch.ensure_pod_and_process(source_url="https://test.m3u8")
    runpod_mock.terminate.assert_any_call("newpod")


@respx.mock
def test_post_process_500_raises_with_body_in_message(
    orch_factory, runpod_mock,
):
    respx.get("https://newpod-8080.proxy.runpod.net/healthz").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )
    respx.post("https://newpod-8080.proxy.runpod.net/process").mock(
        return_value=httpx.Response(
            500, json={"error": "PUBLIC_BASE_URL not configured"}
        )
    )

    orch = orch_factory(runpod_mock)
    with pytest.raises(OrchestratorError, match="PUBLIC_BASE_URL not configured"):
        orch.ensure_pod_and_process(source_url="https://test.m3u8")
    runpod_mock.terminate.assert_any_call("newpod")


# ---- 6. /process_status polling --------------------------------------------

@respx.mock
def test_wait_for_playlist_ready_returns_when_ready_true(
    orch_factory, runpod_mock,
):
    respx.get("https://newpod-8080.proxy.runpod.net/healthz").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )
    respx.post("https://newpod-8080.proxy.runpod.net/process").mock(
        return_value=httpx.Response(200, json={
            "hls_url": "https://newpod-8080.proxy.runpod.net/hls/playlist.m3u8",
            "state": "running",
            "started_at": 1000.0,
            "source_url": "https://test.m3u8",
        })
    )
    # First two calls return not-ready, third returns ready.
    respx.get("https://newpod-8080.proxy.runpod.net/process_status").mock(
        side_effect=make_status_side_effect(ready_after_calls=3),
    )

    sleeps = []
    orch = orch_factory(runpod_mock, sleep_fn=lambda s: sleeps.append(s))
    result = orch.ensure_pod_and_process(source_url="https://test.m3u8")
    assert result.hls_url.endswith("/playlist.m3u8")
    # Two not-ready polls -> two sleeps between them (third call returns ready,
    # loop exits without sleeping again). Healthcheck might add 0 sleeps too.
    playlist_sleeps = [s for s in sleeps if s == 0.01]
    assert len(playlist_sleeps) >= 2


@respx.mock
def test_wait_for_playlist_ready_raises_on_state_failed(
    orch_factory, runpod_mock,
):
    respx.get("https://newpod-8080.proxy.runpod.net/healthz").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )
    respx.post("https://newpod-8080.proxy.runpod.net/process").mock(
        return_value=httpx.Response(200, json={
            "hls_url": "https://newpod-8080.proxy.runpod.net/hls/playlist.m3u8",
            "state": "running",
            "started_at": 1000.0,
            "source_url": "https://test.m3u8",
        })
    )
    respx.get("https://newpod-8080.proxy.runpod.net/process_status").mock(
        return_value=httpx.Response(200, json={
            "state": "failed",
            "hls_url": "https://newpod-8080.proxy.runpod.net/hls/playlist.m3u8",
            "source_url": "https://test.m3u8",
            "started_at": 1000.0,
            "elapsed_s": 5.0,
            "playlist_ready": False,
            "n_segments": 0,
            "pipeline_returncode": 1,
            "error_type": "TRTBuildError",
            "error_msg": "engine build failed",
            "stderr_tail": "boom",
            "traceback": None,
        })
    )

    orch = orch_factory(runpod_mock)
    with pytest.raises(
        OrchestratorError, match="FAILED.*TRTBuildError.*engine build failed",
    ):
        orch.ensure_pod_and_process(source_url="https://test.m3u8")
    runpod_mock.terminate.assert_any_call("newpod")


@respx.mock
def test_wait_for_playlist_ready_returns_false_on_timeout(
    orch_factory, runpod_mock,
):
    respx.get("https://newpod-8080.proxy.runpod.net/healthz").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )
    respx.post("https://newpod-8080.proxy.runpod.net/process").mock(
        return_value=httpx.Response(200, json={
            "hls_url": "https://newpod-8080.proxy.runpod.net/hls/playlist.m3u8",
            "state": "running",
            "started_at": 1000.0,
            "source_url": "https://test.m3u8",
        })
    )
    # Always not-ready -> loop hits deadline.
    respx.get("https://newpod-8080.proxy.runpod.net/process_status").mock(
        return_value=httpx.Response(200, json={
            "state": "running",
            "hls_url": "https://newpod-8080.proxy.runpod.net/hls/playlist.m3u8",
            "source_url": "https://test.m3u8",
            "started_at": 1000.0,
            "elapsed_s": 30.0,
            "playlist_ready": False,
            "n_segments": 0,
            "pipeline_returncode": None,
            "error_type": None,
            "error_msg": None,
            "stderr_tail": None,
            "traceback": None,
        })
    )

    orch = orch_factory(
        runpod_mock,
        playlist_ready_timeout_s=2.0,
        now_fn=make_now_fn(0.0, 0.5),  # walks 0, 0.5, 1.0, 1.5, 2.0 -> deadline at 2.0
    )
    with pytest.raises(OrchestratorError, match="never ready"):
        orch.ensure_pod_and_process(source_url="https://test.m3u8")
    runpod_mock.terminate.assert_any_call("newpod")


# ---- 7. Orphan termination -------------------------------------------------

def test_terminate_orphans_removes_pods_older_than_cap(
    orch_factory, runpod_mock, tmp_path,
):
    state = CloudState(path=tmp_path / "state.json")
    now = time.time()
    state.add_pod(PodRecord(pod_id="old1", public_url="x", created_at=now - 7 * 3600))
    state.add_pod(PodRecord(pod_id="fresh1", public_url="y", created_at=now - 60))
    state.add_pod(PodRecord(pod_id="fresh2", public_url="z", created_at=now - 600))
    state.save()

    orch = orch_factory(runpod_mock, state=state)
    count = orch.terminate_orphans(hard_cap_s=6 * 3600)
    assert count == 1
    runpod_mock.terminate.assert_called_once_with("old1")
    remaining = {p.pod_id for p in state.active_pods()}
    assert remaining == {"fresh1", "fresh2"}


# ---- 8. terminate_pod swallows errors --------------------------------------

def test_terminate_pod_swallows_runpod_errors(
    orch_factory, runpod_mock, tmp_path,
):
    state = CloudState(path=tmp_path / "state.json")
    state.add_pod(PodRecord(pod_id="doomed", public_url="x", created_at=time.time()))
    state.save()
    runpod_mock.terminate.side_effect = RunPodError("api timeout")

    orch = orch_factory(runpod_mock, state=state)
    # Should not propagate.
    orch.terminate_pod("doomed")
    # Pod removed from state regardless.
    assert state.get_pod("doomed") is None


# ---- 9. shutdown_all -------------------------------------------------------

def test_shutdown_all_terminates_every_active_pod(
    orch_factory, runpod_mock, tmp_path,
):
    state = CloudState(path=tmp_path / "state.json")
    now = time.time()
    state.add_pod(PodRecord(pod_id="a", public_url="x", created_at=now))
    state.add_pod(PodRecord(pod_id="b", public_url="y", created_at=now))
    state.add_pod(PodRecord(pod_id="c", public_url="z", created_at=now))
    state.save()

    orch = orch_factory(runpod_mock, state=state)
    count = orch.shutdown_all()
    assert count == 3
    assert state.active_pods() == []
    terminated_ids = {call.args[0] for call in runpod_mock.terminate.call_args_list}
    assert terminated_ids == {"a", "b", "c"}


# ---- 10. hls_url stored on pod record --------------------------------------

@respx.mock
def test_orchestrator_stores_hls_url_on_pod_record(
    orch_factory, runpod_mock, tmp_path,
):
    state = CloudState(path=tmp_path / "state.json")
    expected_hls = "https://newpod-8080.proxy.runpod.net/hls/playlist.m3u8"

    respx.get("https://newpod-8080.proxy.runpod.net/healthz").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )
    respx.post("https://newpod-8080.proxy.runpod.net/process").mock(
        return_value=httpx.Response(200, json={
            "hls_url": expected_hls,
            "state": "running",
            "started_at": 1000.0,
            "source_url": "https://test.m3u8",
        })
    )
    respx.get("https://newpod-8080.proxy.runpod.net/process_status").mock(
        side_effect=make_status_side_effect(ready_after_calls=1),
    )

    orch = orch_factory(runpod_mock, state=state)
    orch.ensure_pod_and_process(source_url="https://test.m3u8")
    pod = state.get_pod("newpod")
    assert pod is not None
    assert pod.hls_url == expected_hls
