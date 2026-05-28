"""Persistent state for cloud pod lifecycle (Pillar 3.6).

Single-user MVP. Stores active pod records under ~/.castbooster/cloud_state.json.
The orchestrator uses this to:
  - Reuse a healthy pod within a 30s no-recast grace window.
  - Detect + terminate orphans on startup (older than 6h).
  - Survive extension restart without leaking pods.

Schema growth pattern (Pillar 3.6 amendments 2026-05-28):
  - Original fields: pod_id, public_url, created_at.
  - New fields: last_heartbeat, idle_until, hls_url.
  - Backward-compatible load: missing keys default to None.

Architecture lock (Abed 2026-05-28): cloud is the only smooth-motion path
forever. See docs/superpowers/specs/2026-05-28-pillar-3.6-tasks-13-26-amendments-design.md.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class PodRecord:
    pod_id: str
    public_url: str
    created_at: float
    last_heartbeat: float | None = None
    idle_until: float | None = None
    hls_url: str | None = None


@dataclass
class CloudState:
    path: Path
    _pods: list[PodRecord] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.path.exists():
            self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        pods = data.get("pods", [])
        # Backward-compat: pre-amendment files only have pod_id/public_url/created_at.
        # Build records explicitly so unknown keys are dropped + missing keys default.
        out: list[PodRecord] = []
        for p in pods:
            if not isinstance(p, dict):
                continue
            try:
                out.append(PodRecord(
                    pod_id=p["pod_id"],
                    public_url=p["public_url"],
                    created_at=float(p["created_at"]),
                    last_heartbeat=p.get("last_heartbeat"),
                    idle_until=p.get("idle_until"),
                    hls_url=p.get("hls_url"),
                ))
            except (KeyError, TypeError, ValueError):
                # Corrupted entry — skip rather than crash startup.
                continue
        self._pods = out

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"pods": [asdict(p) for p in self._pods]}
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def active_pods(self) -> list[PodRecord]:
        return list(self._pods)

    def add_pod(self, pod: PodRecord) -> None:
        # Idempotent on pod_id — if already present, replace.
        self._pods = [p for p in self._pods if p.pod_id != pod.pod_id] + [pod]

    def remove_pod(self, pod_id: str) -> None:
        self._pods = [p for p in self._pods if p.pod_id != pod_id]

    def get_pod(self, pod_id: str) -> PodRecord | None:
        for p in self._pods:
            if p.pod_id == pod_id:
                return p
        return None

    def set_idle_until(self, pod_id: str, ts: float | None) -> None:
        """Set the 'must not terminate before' timestamp. None clears the grace.

        No-op if pod_id not in state — the orchestrator may have already
        terminated + removed it. Auto-saves.
        """
        for i, p in enumerate(self._pods):
            if p.pod_id == pod_id:
                self._pods[i] = PodRecord(
                    pod_id=p.pod_id,
                    public_url=p.public_url,
                    created_at=p.created_at,
                    last_heartbeat=p.last_heartbeat,
                    idle_until=ts,
                    hls_url=p.hls_url,
                )
                self.save()
                return

    def touch_heartbeat(self, pod_id: str, ts: float | None) -> None:
        """Update the last successful healthcheck timestamp. No-op if missing.
        Auto-saves."""
        for i, p in enumerate(self._pods):
            if p.pod_id == pod_id:
                self._pods[i] = PodRecord(
                    pod_id=p.pod_id,
                    public_url=p.public_url,
                    created_at=p.created_at,
                    last_heartbeat=ts,
                    idle_until=p.idle_until,
                    hls_url=p.hls_url,
                )
                self.save()
                return

    def orphans_older_than(
        self, seconds: float, now: float | None = None,
    ) -> list[PodRecord]:
        """Pods whose created_at is older than `seconds` from now."""
        now = now if now is not None else time.time()
        return [p for p in self._pods if (now - p.created_at) >= seconds]


def default_state_path() -> Path:
    """Standard MVP location: ~/.castbooster/cloud_state.json."""
    return Path.home() / ".castbooster" / "cloud_state.json"
