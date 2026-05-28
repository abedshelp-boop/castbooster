"""Cloud RIFE VFI orchestration package (Pillar 3.6).

Modules:
- runpod_client: REST wrapper for RunPod pod lifecycle (Task 14).
- state: persistent cloud_state.json (Task 15).
- orchestrator: ensure_pod_and_process + idle cache + orphan termination (Task 16).
- cloud_cast: top-level entry from proxy._handle_cast (Task 17).

Architecture lock (Abed 2026-05-28): cloud is the only smooth-motion path,
forever. No local-RIFE fallback. See docs/superpowers/specs/2026-05-28-pillar-3.6-tasks-13-26-amendments-design.md.
"""
