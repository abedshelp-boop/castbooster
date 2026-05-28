"""Single-call entry point for the proxy._handle_cast cloud branch.

Wraps CloudOrchestrator with environment-driven config loading and
error translation. Returns a CloudCastResult instead of raising, so the
existing proxy decision tree (Pillar 3.4 + 3.5) can fall back gracefully
to passthrough on cloud failure.

Architecture lock (Abed 2026-05-28): cloud is the only smooth-motion
path forever. See docs/superpowers/specs/2026-05-28-pillar-3.6-tasks-13-26-amendments-design.md.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from app.castbooster.cloud.orchestrator import (
    CloudOrchestrator,
    OrchestratorError,
)
from app.castbooster.cloud.runpod_client import RunPodClient
from app.castbooster.cloud.state import CloudState, default_state_path

log = logging.getLogger(__name__)

DEFAULT_IMAGE = "ghcr.io/abedshelp-boop/castbooster-cloud-worker:v0.3.1"
DEFAULT_GPU_TYPES = (
    "NVIDIA GeForce RTX 4090,"
    "NVIDIA RTX 6000 Ada Generation,"
    "NVIDIA RTX A6000"
)


@dataclass
class CloudCastResult:
    """Outcome of a cloud cast attempt. Always returned (no raises) so the
    proxy can branch cleanly between success and passthrough fallback.

    Fields:
      ok: True iff the worker has a playlist_ready=true HLS URL.
      hls_url: public URL to feed Chromecast (empty if !ok).
      pod_id: RunPod pod id (empty if !ok). Used by proxy._on_session_end
              to call orchestrator.terminate_pod(pod_id) on cast end.
      error: short, popup-displayable failure reason (empty if ok).
      warming_status: last-known stage during the warmup ("booting",
                      "calling_process", "waiting_playlist", etc.). Used
                      by get_session_status to drive the popup card.
    """
    ok: bool
    hls_url: str = ""
    pod_id: str = ""
    error: str = ""
    warming_status: str = ""


class CloudConfigError(ValueError):
    """Raised by build_orchestrator when required env vars are missing."""


def _parse_gpu_types(raw: str) -> list[str]:
    """Comma-separated → list[str], whitespace-stripped, empty entries dropped."""
    return [s.strip() for s in raw.split(",") if s.strip()]


def build_orchestrator() -> CloudOrchestrator:
    """Construct an orchestrator from environment variables.

    Raises CloudConfigError if CLOUD_API_KEY or RUNPOD_API_KEY is missing.
    Other env vars have sensible defaults (image + gpu_types).
    """
    cloud_api_key = os.environ.get("CLOUD_API_KEY", "").strip()
    runpod_api_key = os.environ.get("RUNPOD_API_KEY", "").strip()
    image = os.environ.get("CLOUD_WORKER_IMAGE", DEFAULT_IMAGE).strip()
    gpu_types_raw = os.environ.get("CLOUD_GPU_TYPES", DEFAULT_GPU_TYPES)
    gpu_types = _parse_gpu_types(gpu_types_raw)

    if not cloud_api_key:
        raise CloudConfigError("CLOUD_API_KEY env var is not set")
    if not runpod_api_key:
        raise CloudConfigError("RUNPOD_API_KEY env var is not set")
    if not gpu_types:
        raise CloudConfigError(
            "CLOUD_GPU_TYPES env var parsed to an empty priority list"
        )

    client = RunPodClient(api_key=runpod_api_key)
    state = CloudState(path=default_state_path())
    return CloudOrchestrator(
        runpod=client,
        state=state,
        cloud_api_key=cloud_api_key,
        image=image,
        gpu_types=gpu_types,
        runpod_api_key=runpod_api_key,
    )


def cloud_cast(
    source_url: str,
    source_headers: dict[str, str] | None = None,
) -> CloudCastResult:
    """Top-level cloud cast operation. NEVER raises — returns a structured
    CloudCastResult either way so the proxy can decide whether to play
    cloud HLS or fall back to passthrough.

    Called from proxy._handle_cast via loop.run_in_executor (orchestrator
    methods are sync httpx).
    """
    try:
        orch = build_orchestrator()
    except CloudConfigError as e:
        log.warning("cloud_cast: config missing — %s", e)
        return CloudCastResult(
            ok=False,
            error=str(e),
            warming_status="config_error",
        )

    try:
        result = orch.ensure_pod_and_process(
            source_url=source_url,
            source_headers=source_headers,
        )
    except OrchestratorError as e:
        log.warning("cloud_cast: orchestrator failed — %s", e)
        return CloudCastResult(
            ok=False,
            error=str(e),
            warming_status="failed",
        )
    except Exception as e:
        # Defense in depth — never let the proxy see an exception bubble up.
        log.exception("cloud_cast: unexpected exception")
        return CloudCastResult(
            ok=False,
            error=f"unexpected error: {type(e).__name__}: {e}",
            warming_status="failed",
        )

    return CloudCastResult(
        ok=True,
        hls_url=result.hls_url,
        pod_id=result.pod_id,
        warming_status="streaming",
    )
