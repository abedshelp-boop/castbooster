"""Cloud pod orchestrator (Pillar 3.6).

Owns the pod-lifecycle state machine:
  - ensure_pod_and_process: top-level entry called by cloud_cast.
  - _get_or_create_pod: idle-cache reuse + healthcheck + create-new.
  - _wait_for_playlist_ready: polls /process_status until ready or timeout.
  - terminate_pod: explicit shutdown (called from _on_session_end).
  - terminate_orphans: startup cleanup of stale pods.
  - shutdown_all: atexit cleanup.

v0.3.0 contract (amended 2026-05-28): /process returns 1s with the URL,
but the playlist has zero segments until ~30-60s later. We MUST poll
/process_status for playlist_ready=true before handing the URL to
Chromecast -- otherwise Chromecast hits a missing playlist and bails.

Architecture lock: cloud is the only smooth-motion path forever. See
docs/superpowers/specs/2026-05-28-pillar-3.6-tasks-13-26-amendments-design.md.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

from app.castbooster.cloud.runpod_client import (
    PodInfo,
    RunPodClient,
    RunPodError,
)
from app.castbooster.cloud.state import (
    CloudState,
    PodRecord,
)

log = logging.getLogger(__name__)


class OrchestratorError(Exception):
    """Cloud cast failed at a level the proxy should surface to popup +
    fall through to passthrough."""


@dataclass(frozen=True)
class OrchestrationResult:
    """Returned from ensure_pod_and_process. The proxy wires hls_url into
    cm.play() and uses pod_id for the on_session_end terminate."""
    hls_url: str
    pod_id: str


# Defaults are amendable via env or constructor -- see Lock 5 in amendments doc.
DEFAULT_HEALTHZ_BOOT_TIMEOUT_S = 360.0   # cold pull can be 3-6min
DEFAULT_HEALTHZ_POLL_INTERVAL_S = 3.0
DEFAULT_HEALTHZ_REQUEST_TIMEOUT_S = 5.0
DEFAULT_PLAYLIST_READY_TIMEOUT_S = 90.0  # v0.3.0 spec criterion #2
DEFAULT_PLAYLIST_READY_POLL_INTERVAL_S = 1.0
DEFAULT_PROCESS_POST_TIMEOUT_S = 10.0     # /process should return in 1s; 10s buffer
DEFAULT_IDLE_CACHE_GRACE_S = 30.0          # see Lock 4
DEFAULT_ORPHAN_HARD_CAP_S = 6 * 3600       # 6h


class CloudOrchestrator:
    def __init__(
        self,
        runpod: RunPodClient,
        state: CloudState,
        cloud_api_key: str,
        image: str,
        gpu_types: list[str],
        runpod_api_key: str,
        *,
        healthz_boot_timeout_s: float = DEFAULT_HEALTHZ_BOOT_TIMEOUT_S,
        healthz_poll_interval_s: float = DEFAULT_HEALTHZ_POLL_INTERVAL_S,
        healthz_request_timeout_s: float = DEFAULT_HEALTHZ_REQUEST_TIMEOUT_S,
        playlist_ready_timeout_s: float = DEFAULT_PLAYLIST_READY_TIMEOUT_S,
        playlist_ready_poll_interval_s: float = DEFAULT_PLAYLIST_READY_POLL_INTERVAL_S,
        process_post_timeout_s: float = DEFAULT_PROCESS_POST_TIMEOUT_S,
        idle_cache_grace_s: float = DEFAULT_IDLE_CACHE_GRACE_S,
        sleep_fn=time.sleep,    # injected for tests
        now_fn=time.time,       # injected for tests
        http_client: httpx.Client | None = None,
    ) -> None:
        if not cloud_api_key:
            raise ValueError("cloud_api_key must be set")
        if not gpu_types:
            raise ValueError("gpu_types must be a non-empty priority list")
        self.runpod = runpod
        self.state = state
        self.cloud_api_key = cloud_api_key
        self.image = image
        self.gpu_types = gpu_types
        self.runpod_api_key = runpod_api_key
        self.healthz_boot_timeout_s = healthz_boot_timeout_s
        self.healthz_poll_interval_s = healthz_poll_interval_s
        self.healthz_request_timeout_s = healthz_request_timeout_s
        self.playlist_ready_timeout_s = playlist_ready_timeout_s
        self.playlist_ready_poll_interval_s = playlist_ready_poll_interval_s
        self.process_post_timeout_s = process_post_timeout_s
        self.idle_cache_grace_s = idle_cache_grace_s
        self._sleep = sleep_fn
        self._now = now_fn
        # Reuse a single Client so each httpx call doesn't repeat ~500ms of
        # env/proxy detection (huge on Windows). Tests share this too --
        # respx intercepts at the transport level so the Client cache doesn't
        # interfere with mocks.
        self._http = http_client if http_client is not None else httpx.Client()

    # ---- Public API ------------------------------------------------------

    def ensure_pod_and_process(
        self,
        source_url: str,
        source_headers: dict[str, str] | None = None,
    ) -> OrchestrationResult:
        """Get or create a healthy pod, POST /process, poll /process_status
        until playlist_ready, return the public HLS URL + pod_id.

        Raises OrchestratorError on any unrecoverable failure (pod boot
        timeout, /process 4xx/5xx after retry, playlist_ready timeout,
        pod transitions to FAILED while polling).
        """
        pod = self._get_or_create_pod()
        # The pod is reserved for THIS cast -- clear any grace from a prior
        # cast so terminate-on-session-end isn't blocked by stale idle_until.
        self.state.set_idle_until(pod.pod_id, None)
        try:
            hls_url = self._post_process_then_wait_ready(
                pod=pod,
                source_url=source_url,
                source_headers=source_headers or {},
            )
        except OrchestratorError:
            # On any process-side failure, terminate the pod -- we don't want
            # to leak a paid-by-minute resource on a busted pipeline.
            self._safe_terminate(pod.pod_id)
            raise
        # Stash the URL so /get_session_status can re-attach without re-
        # running the orchestrator after extension restart.
        self._update_pod_record(pod.pod_id, hls_url=hls_url, last_heartbeat=self._now())
        return OrchestrationResult(hls_url=hls_url, pod_id=pod.pod_id)

    def terminate_pod(self, pod_id: str) -> None:
        """Idempotent explicit terminate, called from _on_session_end.

        Respects the idle-cache grace window -- if idle_until > now, schedule
        rather than terminate. Implementation simplification for MVP: caller
        is responsible for grace management; this method always terminates
        immediately. set_idle_until is the orchestrator's grace mechanism;
        terminate_pod is the kill switch.
        """
        self._safe_terminate(pod_id)

    def terminate_orphans(
        self,
        hard_cap_s: float = DEFAULT_ORPHAN_HARD_CAP_S,
    ) -> int:
        """Terminate any pod older than hard_cap_s. Returns count terminated.
        Called on app startup from main.py."""
        orphans = self.state.orphans_older_than(seconds=hard_cap_s, now=self._now())
        count = 0
        for orphan in orphans:
            self._safe_terminate(orphan.pod_id)
            count += 1
        return count

    def shutdown_all(self) -> int:
        """Best-effort terminate everything in state. atexit hook.
        Returns count terminated."""
        count = 0
        for pod in list(self.state.active_pods()):
            self._safe_terminate(pod.pod_id)
            count += 1
        return count

    def close(self) -> None:
        """Close the underlying httpx Client. Optional -- atexit/gc will
        clean it up, but explicit close avoids ResourceWarning in tests."""
        try:
            self._http.close()
        except Exception:
            pass

    # ---- Internal --------------------------------------------------------

    def _get_or_create_pod(self) -> PodRecord:
        """Reuse a healthy in-grace pod if possible; else terminate stale
        + create new."""
        now = self._now()
        for candidate in self.state.active_pods():
            # Idle-cache reuse: if this pod is in its grace window, prefer
            # reusing it over creating a new one (saves $0.10-0.20).
            grace_still_active = (
                candidate.idle_until is not None
                and candidate.idle_until > now
            )
            if self._healthcheck(candidate.public_url):
                # Refresh heartbeat so set_idle_until sees a recent check.
                self.state.touch_heartbeat(candidate.pod_id, now)
                if grace_still_active:
                    log.info(
                        "reusing cached pod %s (idle_until=%s, %.1fs grace remaining)",
                        candidate.pod_id, candidate.idle_until,
                        candidate.idle_until - now,
                    )
                else:
                    log.info("reusing healthy pod %s (no grace)", candidate.pod_id)
                # Re-fetch from state so caller sees the latest record.
                refreshed = self.state.get_pod(candidate.pod_id)
                return refreshed if refreshed is not None else candidate
            # Unhealthy -- terminate + remove from state.
            log.warning(
                "pod %s failed healthcheck; terminating + removing",
                candidate.pod_id,
            )
            self._safe_terminate(candidate.pod_id)

        # No healthy candidate -- create a new pod.
        return self._create_new_pod()

    def _create_new_pod(self) -> PodRecord:
        info: PodInfo = self.runpod.create_pod(
            image=self.image,
            gpu_types=self.gpu_types,
            env={
                "CLOUD_API_KEY": self.cloud_api_key,
                "RUNPOD_API_KEY": self.runpod_api_key,
            },
            ports=[8080],
        )
        record = PodRecord(
            pod_id=info.pod_id,
            public_url=info.public_url,
            created_at=self._now(),
        )
        self.state.add_pod(record)
        self.state.save()
        log.info(
            "created pod %s on %s (public_url=%s)",
            info.pod_id, info.gpu_type, info.public_url,
        )

        if not self._wait_for_healthy(record.public_url):
            log.error(
                "pod %s never became healthy within %.1fs; terminating",
                record.pod_id, self.healthz_boot_timeout_s,
            )
            self._safe_terminate(record.pod_id)
            raise OrchestratorError(
                f"pod {record.pod_id} never became healthy "
                f"within {self.healthz_boot_timeout_s:.0f}s"
            )
        self.state.touch_heartbeat(record.pod_id, self._now())
        # Re-fetch so the caller sees the heartbeat we just stamped.
        refreshed = self.state.get_pod(record.pod_id)
        return refreshed if refreshed is not None else record

    def _wait_for_healthy(self, public_url: str) -> bool:
        deadline = self._now() + self.healthz_boot_timeout_s
        while self._now() < deadline:
            if self._healthcheck(public_url):
                return True
            self._sleep(self.healthz_poll_interval_s)
        return False

    def _healthcheck(self, public_url: str) -> bool:
        try:
            r = self._http.get(
                f"{public_url.rstrip('/')}/healthz",
                timeout=self.healthz_request_timeout_s,
            )
            return r.status_code == 200
        except httpx.RequestError:
            return False

    def _post_process_then_wait_ready(
        self,
        pod: PodRecord,
        source_url: str,
        source_headers: dict[str, str],
    ) -> str:
        """The two-step async pattern: POST /process (1s) then poll
        /process_status until playlist_ready (90s budget)."""
        # Step 1: POST /process
        post_url = f"{pod.public_url.rstrip('/')}/process"
        post_body = {
            "source_url": source_url,
            "source_headers": source_headers,
        }
        try:
            r = self._http.post(
                post_url,
                headers={
                    "Authorization": f"Bearer {self.cloud_api_key}",
                    "Content-Type": "application/json",
                },
                json=post_body,
                timeout=self.process_post_timeout_s,
            )
        except httpx.RequestError as e:
            raise OrchestratorError(
                f"network error during POST /process: {e}"
            ) from e

        if r.status_code == 409:
            # Already running a different source. Terminate pod + raise --
            # next cast will spin a fresh pod. (We don't auto-retry here
            # because the caller may want a different decision tree.)
            raise OrchestratorError(
                f"pod {pod.pod_id} already running a different source "
                f"(409); aborting cloud cast"
            )
        if r.status_code != 200:
            body = (r.text or "")[:500]
            raise OrchestratorError(
                f"POST /process failed ({r.status_code}): {body}"
            )

        # The async shape -- hls_url is what /process_status will eventually
        # gate on. state=running expected.
        try:
            response_body = r.json()
            hls_url = response_body["hls_url"]
        except (KeyError, ValueError) as e:
            raise OrchestratorError(
                f"POST /process returned 200 but malformed body: {r.text[:200]}"
            ) from e

        # Step 2: poll /process_status until playlist_ready=true
        if not self._wait_for_playlist_ready(pod.public_url):
            raise OrchestratorError(
                f"playlist never ready in {self.playlist_ready_timeout_s:.0f}s "
                f"(pod {pod.pod_id})"
            )

        return hls_url

    def _wait_for_playlist_ready(self, public_url: str) -> bool:
        """Poll GET /process_status until playlist_ready=true OR state=failed
        OR timeout. Returns True on ready, False on timeout. Raises
        OrchestratorError on state=failed (fail-fast)."""
        status_url = f"{public_url.rstrip('/')}/process_status"
        deadline = self._now() + self.playlist_ready_timeout_s
        last_state = None
        while self._now() < deadline:
            try:
                r = self._http.get(
                    status_url,
                    headers={"Authorization": f"Bearer {self.cloud_api_key}"},
                    timeout=self.healthz_request_timeout_s,
                )
            except httpx.RequestError as e:
                log.warning("network error polling /process_status: %s", e)
                self._sleep(self.playlist_ready_poll_interval_s)
                continue

            if r.status_code != 200:
                log.warning(
                    "/process_status returned %s -- body=%s",
                    r.status_code, (r.text or "")[:200],
                )
                self._sleep(self.playlist_ready_poll_interval_s)
                continue

            try:
                snap = r.json()
            except ValueError:
                self._sleep(self.playlist_ready_poll_interval_s)
                continue

            state = snap.get("state")
            if state != last_state:
                log.info("/process_status state=%s (was %s)", state, last_state)
                last_state = state

            if state == "failed":
                error_type = snap.get("error_type") or "<unknown>"
                error_msg = snap.get("error_msg") or "<no message>"
                raise OrchestratorError(
                    f"pipeline FAILED: {error_type}: {error_msg}"
                )

            if snap.get("playlist_ready"):
                return True

            self._sleep(self.playlist_ready_poll_interval_s)

        return False

    def _safe_terminate(self, pod_id: str) -> None:
        """terminate + remove from state, swallowing RunPodError so the
        caller's flow continues regardless."""
        try:
            self.runpod.terminate(pod_id)
        except RunPodError as e:
            log.warning("terminate %s failed (continuing): %s", pod_id, e)
        self.state.remove_pod(pod_id)
        self.state.save()

    def _update_pod_record(
        self,
        pod_id: str,
        *,
        hls_url: str | None = None,
        last_heartbeat: float | None = None,
    ) -> None:
        """In-place patch of optional fields. Auto-saves."""
        record = self.state.get_pod(pod_id)
        if record is None:
            return
        # state.py mutators are pinned to single fields; use the existing
        # helpers + direct fix-up where they don't cover the case.
        if last_heartbeat is not None:
            self.state.touch_heartbeat(pod_id, last_heartbeat)
        if hls_url is not None:
            # No dedicated setter -- re-add via the idempotent path.
            self.state.add_pod(PodRecord(
                pod_id=record.pod_id,
                public_url=record.public_url,
                created_at=record.created_at,
                last_heartbeat=last_heartbeat or record.last_heartbeat,
                idle_until=record.idle_until,
                hls_url=hls_url,
            ))
            self.state.save()
