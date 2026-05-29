"""Tests for app/castbooster/cloud/state.py (Pillar 3.6 Task 15).

Covers the persistent cloud_state.json used by the orchestrator for pod
lifecycle bookkeeping. Uses tmp_path for filesystem isolation.
"""
from __future__ import annotations

import json
from pathlib import Path

from castbooster.cloud.state import (
    CloudState,
    PodRecord,
    default_state_path,
)


def test_default_state_path_is_home_castbooster():
    p = default_state_path()
    assert p.name == "cloud_state.json"
    assert p.parent.name == ".castbooster"
    assert p.parent.parent == Path.home()


def test_empty_state_when_file_missing(tmp_path):
    state = CloudState(path=tmp_path / "nope.json")
    assert state.active_pods() == []


def test_save_and_load_round_trip_with_all_fields(tmp_path):
    p = tmp_path / "cloud_state.json"
    state = CloudState(path=p)
    state.add_pod(PodRecord(
        pod_id="abc",
        public_url="https://abc-8080.proxy.runpod.net",
        created_at=1000.0,
        last_heartbeat=1100.0,
        idle_until=1500.0,
        hls_url="https://abc-8080.proxy.runpod.net/hls/playlist.m3u8",
    ))
    state.save()

    state2 = CloudState(path=p)
    pods = state2.active_pods()
    assert len(pods) == 1
    r = pods[0]
    assert r.pod_id == "abc"
    assert r.public_url == "https://abc-8080.proxy.runpod.net"
    assert r.created_at == 1000.0
    assert r.last_heartbeat == 1100.0
    assert r.idle_until == 1500.0
    assert r.hls_url == "https://abc-8080.proxy.runpod.net/hls/playlist.m3u8"


def test_load_backward_compat_with_original_3_field_schema(tmp_path):
    """A cloud_state.json written by a pre-amendment build only has
    pod_id/public_url/created_at. New fields must default to None."""
    p = tmp_path / "cloud_state.json"
    p.write_text(json.dumps({
        "pods": [{
            "pod_id": "legacy",
            "public_url": "https://x",
            "created_at": 1000.0,
        }],
    }))
    state = CloudState(path=p)
    pods = state.active_pods()
    assert len(pods) == 1
    r = pods[0]
    assert r.pod_id == "legacy"
    assert r.last_heartbeat is None
    assert r.idle_until is None
    assert r.hls_url is None


def test_load_skips_corrupted_entry_instead_of_crashing(tmp_path):
    """A corrupted record (missing required field) must not block other records
    from loading. Useful when the file was hand-edited or partially written."""
    p = tmp_path / "cloud_state.json"
    p.write_text(json.dumps({
        "pods": [
            {"pod_id": "good", "public_url": "https://x", "created_at": 1.0},
            {"pod_id": "missing_url", "created_at": 2.0},  # corrupted
            {"pod_id": "good2", "public_url": "https://y", "created_at": 3.0},
        ],
    }))
    state = CloudState(path=p)
    ids = sorted(pod.pod_id for pod in state.active_pods())
    assert ids == ["good", "good2"]


def test_load_tolerates_invalid_json(tmp_path):
    """File present but unreadable -> empty state, no crash."""
    p = tmp_path / "cloud_state.json"
    p.write_text("not json {")
    state = CloudState(path=p)
    assert state.active_pods() == []


def test_add_pod_is_idempotent_on_pod_id(tmp_path):
    """Re-adding the same pod_id replaces the prior record (no duplicates)."""
    state = CloudState(path=tmp_path / "s.json")
    state.add_pod(PodRecord(pod_id="x", public_url="A", created_at=1.0))
    state.add_pod(PodRecord(pod_id="x", public_url="B", created_at=2.0))
    pods = state.active_pods()
    assert len(pods) == 1
    assert pods[0].public_url == "B"


def test_remove_pod_drops_only_matching_id(tmp_path):
    state = CloudState(path=tmp_path / "s.json")
    state.add_pod(PodRecord(pod_id="a", public_url="x", created_at=1.0))
    state.add_pod(PodRecord(pod_id="b", public_url="y", created_at=2.0))
    state.remove_pod("a")
    assert [p.pod_id for p in state.active_pods()] == ["b"]


def test_get_pod_returns_record_or_none(tmp_path):
    state = CloudState(path=tmp_path / "s.json")
    state.add_pod(PodRecord(pod_id="a", public_url="x", created_at=1.0))
    assert state.get_pod("a") is not None
    assert state.get_pod("missing") is None


def test_set_idle_until_updates_and_saves(tmp_path):
    p = tmp_path / "s.json"
    state = CloudState(path=p)
    state.add_pod(PodRecord(pod_id="a", public_url="x", created_at=1.0))
    state.save()

    state.set_idle_until("a", 5000.0)
    assert state.get_pod("a").idle_until == 5000.0

    # Verify auto-save: a freshly loaded state sees the update.
    state2 = CloudState(path=p)
    assert state2.get_pod("a").idle_until == 5000.0


def test_set_idle_until_with_none_clears_grace(tmp_path):
    state = CloudState(path=tmp_path / "s.json")
    state.add_pod(PodRecord(
        pod_id="a", public_url="x", created_at=1.0, idle_until=5000.0,
    ))
    state.set_idle_until("a", None)
    assert state.get_pod("a").idle_until is None


def test_set_idle_until_is_noop_for_unknown_pod(tmp_path):
    """No exception when the pod has already been removed."""
    state = CloudState(path=tmp_path / "s.json")
    state.set_idle_until("never-existed", 9999.0)  # must not raise
    assert state.active_pods() == []


def test_touch_heartbeat_updates_and_saves(tmp_path):
    p = tmp_path / "s.json"
    state = CloudState(path=p)
    state.add_pod(PodRecord(pod_id="a", public_url="x", created_at=1.0))
    state.save()

    state.touch_heartbeat("a", 2500.0)
    state2 = CloudState(path=p)
    assert state2.get_pod("a").last_heartbeat == 2500.0


def test_orphans_older_than_returns_only_old_pods(tmp_path):
    now = 10000.0
    state = CloudState(path=tmp_path / "s.json")
    state.add_pod(PodRecord(
        pod_id="old", public_url="x", created_at=now - 7 * 3600,
    ))
    state.add_pod(PodRecord(
        pod_id="fresh", public_url="y", created_at=now - 60,
    ))
    orphans = state.orphans_older_than(seconds=6 * 3600, now=now)
    assert [p.pod_id for p in orphans] == ["old"]
