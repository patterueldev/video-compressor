"""Lease expiry: reconciliation requeues live-safe jobs and parks risky ones."""

from __future__ import annotations

from videocompressor.controller.constants import LEASE_TTL_SECONDS
from videocompressor.core.models import JobState

CLIP = "2024-06-Ride/GS010001.MP4"


def _messages(store, substring: str) -> list[str]:
    return [event.message for event in store.list_events() if substring in event.message]


def test_expired_lease_requeues_and_releases_worker(server, api, clock, seed_clip, manifest, store):
    api.register("homeserver")
    seed_clip(CLIP)
    status, _ = api.post("/jobs/claim", {"worker_id": "homeserver"})
    assert status == 200

    clock.advance(LEASE_TTL_SECONDS + 1)
    server.sweep_once()

    record = manifest.list_clips()[0]
    assert record.state is JobState.QUEUED
    assert record.worker_id is None
    assert _messages(store, "lease expired") == [f"requeued {CLIP}: lease expired"]
    assert _messages(store, "offline") == ["worker homeserver offline"]
    expired = [event for event in store.list_events() if "lease expired" in event.message]
    assert [event.worker_id for event in expired] == ["homeserver"]

    # an offline worker's job becomes claimable after reconciliation
    api.register("other")
    status, body = api.post("/jobs/claim", {"worker_id": "other"})
    assert status == 200
    assert body["job"]["relative_path"] == CLIP


def test_expired_worker_reports_are_rejected(server, api, clock, seed_clip):
    api.register("a")
    seed_clip(CLIP)
    assert api.post("/jobs/claim", {"worker_id": "a"})[0] == 200
    clock.advance(LEASE_TTL_SECONDS + 1)
    server.sweep_once()

    status, body = api.post(
        "/workers/a/heartbeat",
        {"active": {"root_id": "archive", "relative_path": CLIP}},
    )
    assert status == 409 and body["error"]["code"] == "lease_conflict"
    status, body = api.post(
        "/jobs/complete",
        {
            "worker_id": "a",
            "root_id": "archive",
            "relative_path": CLIP,
            "output": {"size_bytes": 1, "digest": "x"},
        },
    )
    assert status == 409


def test_expired_during_replacing_parks_in_review(server, api, clock, seed_clip, manifest, store):
    api.register("a")
    seed_clip(CLIP)
    assert api.post("/jobs/claim", {"worker_id": "a"})[0] == 200
    manifest.update_state("archive", CLIP, JobState.REPLACING)

    clock.advance(LEASE_TTL_SECONDS + 1)
    server.sweep_once()

    record = manifest.list_clips()[0]
    assert record.state is JobState.REVIEW
    assert "reconcile the source" in (record.error or "")
    parked = [event for event in store.list_events() if "parked in review" in event.message]
    assert parked and parked[0].worker_id == "a"

    # a review-parked clip is not claimable
    api.register("b")
    status, body = api.post("/jobs/claim", {"worker_id": "b"})
    assert status == 204 and body is None


def test_heartbeat_within_renewed_lease_keeps_job(server, api, clock, seed_clip, manifest, store):
    api.register("a")
    seed_clip(CLIP)
    assert api.post("/jobs/claim", {"worker_id": "a"})[0] == 200

    clock.advance(80)
    status, _ = api.post(
        "/workers/a/heartbeat",
        {"active": {"root_id": "archive", "relative_path": CLIP}},
    )
    assert status == 200
    clock.advance(80)  # t+160: expired without the renewal
    server.sweep_once()

    assert manifest.list_clips()[0].state is JobState.CLAIMED
    assert _messages(store, "lease expired") == []
