"""Tests for the P3.6 cloud-pod lifecycle hooks in main.py:
    - _terminate_cloud_orphans_on_startup (called before start_proxy)
    - _shutdown_cloud_pods_on_exit (registered via atexit)

Both must be env-gated (CLOUD_API_KEY + RUNPOD_API_KEY required) and
exception-swallowing (must NEVER block app startup or pollute atexit).
"""
from unittest.mock import MagicMock, patch

import pytest

from castbooster.main import (
    _shutdown_cloud_pods_on_exit,
    _terminate_cloud_orphans_on_startup,
)


@pytest.fixture(autouse=True)
def _clear_cloud_env(monkeypatch):
    """Strip CLOUD_API_KEY + RUNPOD_API_KEY before each test so the env
    state is deterministic. Tests that want them set monkeypatch them in."""
    monkeypatch.delenv("CLOUD_API_KEY", raising=False)
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)


def test_terminate_orphans_noop_without_cloud_env():
    """No env -> no orchestrator build, no calls. Must not raise."""
    with patch("castbooster.cloud.cloud_cast.build_orchestrator") as mock_build:
        _terminate_cloud_orphans_on_startup()
        mock_build.assert_not_called()


def test_terminate_orphans_calls_orchestrator_when_env_set(monkeypatch):
    monkeypatch.setenv("CLOUD_API_KEY", "ck")
    monkeypatch.setenv("RUNPOD_API_KEY", "rp")
    fake_orch = MagicMock()
    fake_orch.terminate_orphans.return_value = 2
    with patch("castbooster.cloud.cloud_cast.build_orchestrator", return_value=fake_orch):
        _terminate_cloud_orphans_on_startup()
    fake_orch.terminate_orphans.assert_called_once_with(hard_cap_s=6 * 3600)


def test_terminate_orphans_swallows_exceptions(monkeypatch):
    """Orchestrator construction or terminate_orphans raising must NOT
    propagate -- startup must continue."""
    monkeypatch.setenv("CLOUD_API_KEY", "ck")
    monkeypatch.setenv("RUNPOD_API_KEY", "rp")
    with patch(
        "castbooster.cloud.cloud_cast.build_orchestrator",
        side_effect=RuntimeError("simulated"),
    ):
        _terminate_cloud_orphans_on_startup()  # must not raise


def test_shutdown_all_noop_without_cloud_env():
    with patch("castbooster.cloud.cloud_cast.build_orchestrator") as mock_build:
        _shutdown_cloud_pods_on_exit()
        mock_build.assert_not_called()


def test_shutdown_all_calls_orchestrator_when_env_set(monkeypatch):
    monkeypatch.setenv("CLOUD_API_KEY", "ck")
    monkeypatch.setenv("RUNPOD_API_KEY", "rp")
    fake_orch = MagicMock()
    fake_orch.shutdown_all.return_value = 3
    with patch("castbooster.cloud.cloud_cast.build_orchestrator", return_value=fake_orch):
        _shutdown_cloud_pods_on_exit()
    fake_orch.shutdown_all.assert_called_once()


def test_shutdown_all_swallows_exceptions(monkeypatch):
    monkeypatch.setenv("CLOUD_API_KEY", "ck")
    monkeypatch.setenv("RUNPOD_API_KEY", "rp")
    fake_orch = MagicMock()
    fake_orch.shutdown_all.side_effect = RuntimeError("simulated")
    with patch("castbooster.cloud.cloud_cast.build_orchestrator", return_value=fake_orch):
        _shutdown_cloud_pods_on_exit()  # must not raise
