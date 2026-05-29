from unittest.mock import MagicMock

import pytest

from castbooster.cloud.cloud_cast import (
    CloudConfigError,
    DEFAULT_IMAGE,
    _parse_gpu_types,
    build_orchestrator,
    cloud_cast,
)
from castbooster.cloud.orchestrator import (
    OrchestrationResult,
    OrchestratorError,
)


def test_parse_gpu_types_splits_and_strips():
    assert _parse_gpu_types("a, b ,c") == ["a", "b", "c"]


def test_parse_gpu_types_drops_empty_entries():
    assert _parse_gpu_types("a,,b,  ,c") == ["a", "b", "c"]


def test_parse_gpu_types_empty_string_returns_empty_list():
    assert _parse_gpu_types("") == []


def test_build_orchestrator_raises_when_cloud_api_key_missing(monkeypatch):
    monkeypatch.delenv("CLOUD_API_KEY", raising=False)
    monkeypatch.setenv("RUNPOD_API_KEY", "rp")
    with pytest.raises(CloudConfigError, match="CLOUD_API_KEY"):
        build_orchestrator()


def test_build_orchestrator_raises_when_runpod_api_key_missing(monkeypatch):
    monkeypatch.setenv("CLOUD_API_KEY", "ck")
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    with pytest.raises(CloudConfigError, match="RUNPOD_API_KEY"):
        build_orchestrator()


def test_build_orchestrator_raises_when_gpu_types_empty(monkeypatch):
    monkeypatch.setenv("CLOUD_API_KEY", "ck")
    monkeypatch.setenv("RUNPOD_API_KEY", "rp")
    monkeypatch.setenv("CLOUD_GPU_TYPES", " , , ")
    with pytest.raises(CloudConfigError, match="empty priority list"):
        build_orchestrator()


def test_build_orchestrator_returns_orchestrator_with_env_config(monkeypatch, tmp_path):
    monkeypatch.setenv("CLOUD_API_KEY", "ck")
    monkeypatch.setenv("RUNPOD_API_KEY", "rp")
    monkeypatch.setenv("CLOUD_WORKER_IMAGE", "ghcr.io/test/worker:v0.9.9")
    monkeypatch.setenv("CLOUD_GPU_TYPES", "G1,G2")
    # Redirect state path so tests don't write to ~/.castbooster/
    monkeypatch.setattr(
        "castbooster.cloud.cloud_cast.default_state_path",
        lambda: tmp_path / "state.json",
    )
    orch = build_orchestrator()
    assert orch.cloud_api_key == "ck"
    assert orch.runpod_api_key == "rp"
    assert orch.image == "ghcr.io/test/worker:v0.9.9"
    assert orch.gpu_types == ["G1", "G2"]


def test_build_orchestrator_falls_back_to_default_image(monkeypatch, tmp_path):
    monkeypatch.setenv("CLOUD_API_KEY", "ck")
    monkeypatch.setenv("RUNPOD_API_KEY", "rp")
    monkeypatch.delenv("CLOUD_WORKER_IMAGE", raising=False)
    monkeypatch.setattr(
        "castbooster.cloud.cloud_cast.default_state_path",
        lambda: tmp_path / "state.json",
    )
    orch = build_orchestrator()
    assert orch.image == DEFAULT_IMAGE


def test_cloud_cast_returns_config_error_when_env_missing(monkeypatch):
    monkeypatch.delenv("CLOUD_API_KEY", raising=False)
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    result = cloud_cast(source_url="x")
    assert not result.ok
    assert "CLOUD_API_KEY" in result.error or "RUNPOD_API_KEY" in result.error
    assert result.warming_status == "config_error"


def test_cloud_cast_returns_ok_on_orchestrator_success(monkeypatch):
    fake_orch = MagicMock()
    fake_orch.ensure_pod_and_process.return_value = OrchestrationResult(
        hls_url="https://pod-8080.proxy.runpod.net/hls/playlist.m3u8",
        pod_id="pod123",
    )
    monkeypatch.setattr(
        "castbooster.cloud.cloud_cast.build_orchestrator",
        lambda: fake_orch,
    )
    result = cloud_cast(
        source_url="https://egydead.example/anime.m3u8",
        source_headers={"Cookie": "session=abc"},
    )
    assert result.ok
    assert result.hls_url == "https://pod-8080.proxy.runpod.net/hls/playlist.m3u8"
    assert result.pod_id == "pod123"
    assert result.warming_status == "streaming"
    assert result.error == ""
    fake_orch.ensure_pod_and_process.assert_called_with(
        source_url="https://egydead.example/anime.m3u8",
        source_headers={"Cookie": "session=abc"},
    )


def test_cloud_cast_returns_failure_on_orchestrator_error(monkeypatch):
    fake_orch = MagicMock()
    fake_orch.ensure_pod_and_process.side_effect = OrchestratorError(
        "playlist never ready in 90s"
    )
    monkeypatch.setattr(
        "castbooster.cloud.cloud_cast.build_orchestrator",
        lambda: fake_orch,
    )
    result = cloud_cast(source_url="x")
    assert not result.ok
    assert "playlist never ready" in result.error
    assert result.warming_status == "failed"


def test_cloud_cast_catches_unexpected_exception(monkeypatch):
    fake_orch = MagicMock()
    fake_orch.ensure_pod_and_process.side_effect = RuntimeError("boom")
    monkeypatch.setattr(
        "castbooster.cloud.cloud_cast.build_orchestrator",
        lambda: fake_orch,
    )
    result = cloud_cast(source_url="x")
    assert not result.ok
    assert "RuntimeError" in result.error
    assert "boom" in result.error
    assert result.warming_status == "failed"
