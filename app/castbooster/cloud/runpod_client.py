"""Thin wrapper around RunPod REST API for pod lifecycle management.

Docs: https://docs.runpod.io/api-reference/pods/create-a-pod

Single-user MVP — sync httpx calls (orchestrator is in-thread, no need
for async here). The orchestrator handles pod-readiness polling via
direct /healthz checks, NOT via RunPod's `currentStatus` field (which
is unreliable per the 2026-05-27 gotcha).

Architecture lock (Abed 2026-05-28): cloud is the only smooth-motion
path forever. No fallback to local. See docs/superpowers/specs/
2026-05-28-pillar-3.6-tasks-13-26-amendments-design.md.
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx


RUNPOD_API_BASE = "https://rest.runpod.io/v1"

# Substrings that indicate stock-out for a given GPU type. When create_pod
# sees these in an error body, it advances to the next gpu_type rather than
# raising — RunPod stock fluctuates hourly, so a priority list trades one
# 500 for a successful pod on the second/third try.
_STOCK_OUT_PATTERNS = (
    "no instances currently available",
    "This machine does not have the resources",
)


class RunPodError(Exception):
    """Raised when a RunPod API call returns a non-success status that we
    can't recover from (auth, malformed request, exhausted gpu priority list)."""


@dataclass(frozen=True)
class PodInfo:
    pod_id: str
    public_url: str
    gpu_type: str  # the one that actually got allocated, for telemetry


class RunPodClient:
    def __init__(self, api_key: str, timeout_s: float = 30.0) -> None:
        if not api_key:
            raise ValueError("RUNPOD_API_KEY must be set")
        self.api_key = api_key
        self.timeout_s = timeout_s

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    def create_pod(
        self,
        image: str,
        gpu_types: list[str],
        env: dict[str, str],
        ports: list[int],
        name: str = "castbooster-cloud-worker",
        container_disk_in_gb: int = 30,
        cloud_type: str = "SECURE",
    ) -> PodInfo:
        """Create a pod, trying each gpu_type in priority order until one
        succeeds or all are stocked out.

        Args:
            gpu_types: priority list of RunPod GPU type slugs. First-available
                wins. Stock-out on one slug advances to the next.
            ports: list of port numbers (ints). Sent to RunPod as
                ["8080/http", ...] strings (note the /http suffix).

        Raises:
            RunPodError on non-stock-out API errors (auth, malformed request,
                etc.) or after exhausting all gpu_types on stock-out.
            ValueError if gpu_types is empty.
        """
        if not gpu_types:
            raise ValueError("gpu_types must be a non-empty priority list")
        last_error = None
        for gpu_type in gpu_types:
            body = {
                "name": name,
                "imageName": image,
                "gpuTypeIds": [gpu_type],
                "containerDiskInGb": container_disk_in_gb,
                "env": env,
                "ports": [f"{p}/http" for p in ports],
                "cloudType": cloud_type,
            }
            try:
                response = httpx.post(
                    f"{RUNPOD_API_BASE}/pods",
                    headers=self._headers(),
                    json=body,
                    timeout=self.timeout_s,
                )
            except httpx.RequestError as e:
                raise RunPodError(f"network error during create_pod: {e}") from e

            if response.status_code < 400:
                data = response.json()
                pod_id = data["id"]
                public_url = f"https://{pod_id}-{ports[0]}.proxy.runpod.net"
                return PodInfo(pod_id=pod_id, public_url=public_url, gpu_type=gpu_type)

            error_text = response.text or ""
            if any(pat in error_text for pat in _STOCK_OUT_PATTERNS):
                # Stock-out on this specific GPU. Try the next in the list.
                last_error = (
                    f"stock-out on {gpu_type!r}: "
                    f"HTTP {response.status_code} {error_text[:200]}"
                )
                continue
            # Non-stock-out failure → bail immediately, don't waste calls.
            raise RunPodError(
                f"create_pod failed on {gpu_type!r} "
                f"({response.status_code}): {error_text[:500]}"
            )

        raise RunPodError(
            f"all_gpu_types_unavailable: exhausted {len(gpu_types)} GPU "
            f"types; last error: {last_error}"
        )

    def terminate(self, pod_id: str) -> None:
        """Stop a pod. Swallows 404 (pod already gone, treat as success).
        Re-raises other errors as RunPodError."""
        try:
            response = httpx.post(
                f"{RUNPOD_API_BASE}/pods/{pod_id}/stop",
                headers=self._headers(),
                timeout=self.timeout_s,
            )
        except httpx.RequestError as e:
            raise RunPodError(f"network error during terminate: {e}") from e

        if response.status_code == 404:
            return  # already terminated, fine
        if response.status_code >= 400:
            raise RunPodError(
                f"terminate failed ({response.status_code}): {response.text[:500]}"
            )
