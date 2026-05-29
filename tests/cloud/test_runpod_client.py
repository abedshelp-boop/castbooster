"""Tests for castbooster.cloud.runpod_client.

Mocks RunPod's REST API via respx so we exercise the wrapper logic
(priority-list retry, body shape, error handling) without burning real
API quota.
"""
from __future__ import annotations

import json

import httpx
import pytest
import respx

from castbooster.cloud.runpod_client import (
    RUNPOD_API_BASE,
    RunPodClient,
    RunPodError,
)


@pytest.fixture
def client():
    return RunPodClient(api_key="test-runpod-key")


def test_init_rejects_empty_api_key():
    with pytest.raises(ValueError, match="RUNPOD_API_KEY"):
        RunPodClient(api_key="")


def test_create_pod_rejects_empty_gpu_types(client):
    with pytest.raises(ValueError, match="gpu_types"):
        client.create_pod(image="x", gpu_types=[], env={}, ports=[8080])


@respx.mock
def test_create_pod_returns_pod_info_on_success(client):
    respx.post(f"{RUNPOD_API_BASE}/pods").mock(
        return_value=httpx.Response(
            201,
            json={"id": "abc123pod"},
        )
    )
    info = client.create_pod(
        image="ghcr.io/test/worker:v0.3.1",
        gpu_types=["NVIDIA GeForce RTX 4090"],
        env={"CLOUD_API_KEY": "secret"},
        ports=[8080],
    )
    assert info.pod_id == "abc123pod"
    assert info.public_url == "https://abc123pod-8080.proxy.runpod.net"
    assert info.gpu_type == "NVIDIA GeForce RTX 4090"


@respx.mock
def test_create_pod_sends_ports_as_http_suffixed_array_and_secure_cloud(client):
    """Body shape: ports: ["8080/http"] array (not joined string); cloudType SECURE."""
    route = respx.post(f"{RUNPOD_API_BASE}/pods").mock(
        return_value=httpx.Response(201, json={"id": "p"})
    )
    client.create_pod(
        image="x",
        gpu_types=["g"],
        env={"K": "V"},
        ports=[8080],
    )
    assert route.called
    raw_body = route.calls[0].request.read().decode()
    payload = json.loads(raw_body)
    assert payload["ports"] == ["8080/http"]
    assert payload["cloudType"] == "SECURE"
    assert payload["gpuTypeIds"] == ["g"]
    assert payload["imageName"] == "x"
    assert payload["env"] == {"K": "V"}
    # Default name + container disk size are included.
    assert payload["name"] == "castbooster-cloud-worker"
    assert payload["containerDiskInGb"] == 30
    # Authorization header is the bearer token.
    auth = route.calls[0].request.headers.get("Authorization")
    assert auth == "Bearer test-runpod-key"


@respx.mock
def test_create_pod_retries_across_priority_list_on_stock_out(client):
    """When the first GPU returns stock-out, advance to the next."""
    responses = [
        httpx.Response(
            500,
            json={"error": "no instances currently available for NVIDIA GeForce RTX 4090"},
        ),
        httpx.Response(201, json={"id": "backup-pod"}),
    ]
    route = respx.post(f"{RUNPOD_API_BASE}/pods").mock(side_effect=responses)
    info = client.create_pod(
        image="x",
        gpu_types=["NVIDIA GeForce RTX 4090", "NVIDIA RTX 6000 Ada Generation"],
        env={},
        ports=[8080],
    )
    assert info.pod_id == "backup-pod"
    assert info.gpu_type == "NVIDIA RTX 6000 Ada Generation"
    assert route.call_count == 2
    # Second call's body should target the backup GPU, not the original.
    second_payload = json.loads(route.calls[1].request.read().decode())
    assert second_payload["gpuTypeIds"] == ["NVIDIA RTX 6000 Ada Generation"]


@respx.mock
def test_create_pod_retries_on_second_stock_out_pattern(client):
    """The 'This machine does not have the resources' phrase also triggers retry."""
    responses = [
        httpx.Response(
            500,
            json={"error": "This machine does not have the resources required."},
        ),
        httpx.Response(201, json={"id": "ok"}),
    ]
    route = respx.post(f"{RUNPOD_API_BASE}/pods").mock(side_effect=responses)
    info = client.create_pod(
        image="x",
        gpu_types=["g1", "g2"],
        env={},
        ports=[8080],
    )
    assert info.pod_id == "ok"
    assert info.gpu_type == "g2"
    assert route.call_count == 2


@respx.mock
def test_create_pod_raises_when_all_gpu_types_unavailable(client):
    respx.post(f"{RUNPOD_API_BASE}/pods").mock(
        return_value=httpx.Response(
            500,
            json={"error": "no instances currently available"},
        )
    )
    with pytest.raises(RunPodError, match="all_gpu_types_unavailable"):
        client.create_pod(
            image="x",
            gpu_types=["g1", "g2", "g3"],
            env={},
            ports=[8080],
        )


@respx.mock
def test_create_pod_raises_immediately_on_non_stock_out_error(client):
    """Auth failure / malformed request — don't burn the whole priority list."""
    route = respx.post(f"{RUNPOD_API_BASE}/pods").mock(
        return_value=httpx.Response(401, json={"error": "invalid api key"})
    )
    with pytest.raises(RunPodError, match="invalid api key"):
        client.create_pod(
            image="x",
            gpu_types=["g1", "g2"],
            env={},
            ports=[8080],
        )
    # Bailed after the first call, did not retry across the list.
    assert route.call_count == 1


@respx.mock
def test_terminate_calls_stop_endpoint(client):
    route = respx.post(f"{RUNPOD_API_BASE}/pods/abc/stop").mock(
        return_value=httpx.Response(200, json={"status": "stopped"})
    )
    client.terminate("abc")
    assert route.called
    auth = route.calls[0].request.headers.get("Authorization")
    assert auth == "Bearer test-runpod-key"


@respx.mock
def test_terminate_swallows_404(client):
    """Already-terminated pods return 404. Treat as success, don't raise."""
    respx.post(f"{RUNPOD_API_BASE}/pods/abc/stop").mock(
        return_value=httpx.Response(404, json={"error": "not found"})
    )
    client.terminate("abc")  # must not raise


@respx.mock
def test_terminate_raises_on_other_errors(client):
    respx.post(f"{RUNPOD_API_BASE}/pods/abc/stop").mock(
        return_value=httpx.Response(500, json={"error": "internal"})
    )
    with pytest.raises(RunPodError, match="500"):
        client.terminate("abc")
