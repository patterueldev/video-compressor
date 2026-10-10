"""Stop/resume: heartbeat stop flag, worker acknowledgement, and safe requeue."""

from __future__ import annotations

from videocompressor.core.models import JobState

CLIP = "2024-06-Ride/GS010001.MP4"
ACTIVE = {"root_id": "archive", "relative_path": CLIP}
COMPLETE_PAYLOAD = {
    "worker_id": "a",
    "root_id": "archive",
    "relative_path": CLIP,
    "output": {"size_bytes": 100, "mtime_ns": 5, "digest": "out"},
}


def _claim(api, worker_id: str) -> None:
    status, _ = api.post("/jobs/claim", {"worker_id": worker_id})
    assert status == 200


def test_stop_ack_via_fail_requeue_and_resume(server, api, seed_clip, manifest, store):
    api.register("a")
    seed_clip(CLIP)
    _claim(api, "a")

    status, _ = api.post("/workers/a/stop")
    assert status == 204
    status, heartbeat = api.post("/workers/a/heartbeat", {"active": ACTIVE})
    assert status == 200 and heartbeat["stop"] is True

    status, _ = api.post(
        "/jobs/fail",
        {
            "worker_id": "a",
            "root_id": "archive",
            "relative_path": CLIP,
            "error": "stopped by operator",
            "disposition": "requeue",
        },
    )
    assert status == 204
    record = manifest.list_clips()[0]
    assert record.state is JobState.QUEUED
    assert record.worker_id is None
    messages = [event.message for event in store.list_events()]
    assert any("requeued" in message and "stopped by operator" in message for message in messages)

    status, heartbeat = api.post("/workers/a/heartbeat", {})
    assert heartbeat["stop"] is False

    # resume: the clip is queued again and can be claimed by another worker
    api.register("b")
    status, body = api.post("/jobs/claim", {"worker_id": "b"})
    assert status == 200
    assert body["job"]["relative_path"] == CLIP


def test_stop_without_active_job_is_a_noop(api):
    api.register("a")
    status, _ = api.post("/workers/a/stop")
    assert status == 204
    status, heartbeat = api.post("/workers/a/heartbeat", {})
    assert heartbeat["stop"] is False
    status, workers = api.get("/workers")
    assert workers["workers"][0]["stop_requested"] is False


def test_stop_cleared_when_job_completes_first(server, api, seed_clip, manifest, store):
    api.register("a")
    seed_clip(CLIP)
    _claim(api, "a")
    assert api.post("/workers/a/stop")[0] == 204

    status, _ = api.post("/jobs/complete", COMPLETE_PAYLOAD)
    assert status == 204
    assert manifest.list_clips()[0].state is JobState.COMPLETE

    status, heartbeat = api.post("/workers/a/heartbeat", {})
    assert heartbeat["stop"] is False
    assert any("stop cleared" in event.message for event in store.list_events())


def test_heartbeat_without_active_requeues_stopped_job(server, api, seed_clip, manifest, store):
    api.register("a")
    seed_clip(CLIP)
    _claim(api, "a")
    assert api.post("/workers/a/stop")[0] == 204

    status, heartbeat = api.post("/workers/a/heartbeat", {})
    assert status == 200 and heartbeat["stop"] is False
    record = manifest.list_clips()[0]
    assert record.state is JobState.QUEUED and record.worker_id is None
    assert any("stopped without reporting" in event.message for event in store.list_events())


def test_resume_clears_stop_flag_without_releasing_held_job(server, api, seed_clip, manifest):
    api.register("a")
    seed_clip(CLIP)
    _claim(api, "a")
    assert api.post("/workers/a/stop")[0] == 204

    status, _ = api.post("/workers/a/resume")
    assert status == 204
    status, heartbeat = api.post("/workers/a/heartbeat", {"active": ACTIVE})
    assert heartbeat["stop"] is False
    assert manifest.list_clips()[0].state is JobState.CLAIMED


def test_stop_and_resume_unknown_worker_404(api):
    status, body = api.post("/workers/ghost/stop")
    assert status == 404 and body["error"]["code"] == "unknown_worker"
    status, body = api.post("/workers/ghost/resume")
    assert status == 404 and body["error"]["code"] == "unknown_worker"
