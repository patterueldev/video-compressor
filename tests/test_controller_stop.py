"""Stop/resume: stop modes, heartbeat delivery, worker acknowledgement, requeue."""

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


def _entry(api, worker_id: str = "a") -> dict:
    status, workers = api.get("/workers")
    assert status == 200
    return next(entry for entry in workers["workers"] if entry["worker_id"] == worker_id)


def test_stop_ack_via_fail_requeue_and_resume(server, api, seed_clip, manifest, store):
    api.register("a")
    seed_clip(CLIP)
    _claim(api, "a")

    status, _ = api.post("/workers/a/stop")
    assert status == 204
    entry = _entry(api)
    assert entry["stop_requested"] is True
    assert entry["stop_mode"] == "cancel"

    status, heartbeat = api.post("/workers/a/heartbeat", {"active": ACTIVE})
    assert status == 200 and heartbeat["stop"] is True
    assert heartbeat["stop_mode"] == "cancel"

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
    assert heartbeat["stop_mode"] is None

    # resume: the clip is queued again and can be claimed by another worker
    api.register("b")
    status, body = api.post("/jobs/claim", {"worker_id": "b"})
    assert status == 200
    assert body["job"]["relative_path"] == CLIP


def test_stop_after_current_persists_until_resume(server, api, seed_clip, manifest):
    api.register("a")
    seed_clip(CLIP)
    _claim(api, "a")

    status, _ = api.post("/workers/a/stop", {"mode": "drain"})
    assert status == 204
    entry = _entry(api)
    assert entry["stop_requested"] is True
    assert entry["stop_mode"] == "drain"

    status, heartbeat = api.post("/workers/a/heartbeat", {"active": ACTIVE})
    assert heartbeat["stop"] is True and heartbeat["stop_mode"] == "drain"
    # drain never cancels: the clip stays claimed until the worker settles it
    assert manifest.get_clip("archive", CLIP).state is JobState.CLAIMED

    # a later stop-now request replaces the pending drain
    status, _ = api.post("/workers/a/stop", {"mode": "cancel"})
    assert status == 204
    status, heartbeat = api.post("/workers/a/heartbeat", {"active": ACTIVE})
    assert heartbeat["stop_mode"] == "cancel"


def test_idle_stop_sets_the_flag_and_is_delivered(api):
    api.register("a")
    status, _ = api.post("/workers/a/stop", {"mode": "drain"})
    assert status == 204
    entry = _entry(api)
    assert entry["stop_requested"] is True
    assert entry["stop_mode"] == "drain"

    status, heartbeat = api.post("/workers/a/heartbeat", {})
    assert status == 200
    assert heartbeat["stop"] is True and heartbeat["stop_mode"] == "drain"

    # the request survives until the worker acts on it or the operator resumes
    assert _entry(api)["stop_requested"] is True


def test_idle_stop_defaults_to_cancel_and_resume_clears(api):
    api.register("a")
    assert api.post("/workers/a/stop")[0] == 204
    status, heartbeat = api.post("/workers/a/heartbeat", {})
    assert heartbeat["stop"] is True and heartbeat["stop_mode"] == "cancel"

    assert api.post("/workers/a/resume")[0] == 204
    entry = _entry(api)
    assert entry["stop_requested"] is False
    assert entry["stop_mode"] is None
    status, heartbeat = api.post("/workers/a/heartbeat", {})
    assert heartbeat["stop"] is False and heartbeat["stop_mode"] is None


def test_invalid_stop_mode_is_rejected(api):
    api.register("a")
    for mode in ("later", 5, ""):
        status, body = api.post("/workers/a/stop", {"mode": mode})
        assert status == 400 and body["error"]["code"] == "bad_request"
    assert _entry(api)["stop_requested"] is False


def test_register_clears_a_pending_stop(api):
    api.register("a")
    assert api.post("/workers/a/stop", {"mode": "drain"})[0] == 204

    api.register("a")  # a restarted node starts fresh

    entry = _entry(api)
    assert entry["stop_requested"] is False
    assert entry["stop_mode"] is None


def test_stop_cleared_when_job_completes_first(server, api, seed_clip, manifest, store):
    api.register("a")
    seed_clip(CLIP)
    _claim(api, "a")
    assert api.post("/workers/a/stop", {"mode": "drain"})[0] == 204

    status, _ = api.post("/jobs/complete", COMPLETE_PAYLOAD)
    assert status == 204
    assert manifest.list_clips()[0].state is JobState.COMPLETE

    status, heartbeat = api.post("/workers/a/heartbeat", {})
    assert heartbeat["stop"] is False and heartbeat["stop_mode"] is None
    assert any("stop cleared" in event.message for event in store.list_events())


def test_heartbeat_without_active_requeues_stopped_job(server, api, seed_clip, manifest, store):
    api.register("a")
    seed_clip(CLIP)
    _claim(api, "a")
    assert api.post("/workers/a/stop")[0] == 204

    status, heartbeat = api.post("/workers/a/heartbeat", {})
    assert status == 200 and heartbeat["stop"] is True
    assert heartbeat["stop_mode"] == "cancel"
    record = manifest.list_clips()[0]
    assert record.state is JobState.QUEUED and record.worker_id is None
    assert any("stopped without reporting" in event.message for event in store.list_events())

    # the abandoned-clip acknowledgement consumed the request
    status, heartbeat = api.post("/workers/a/heartbeat", {})
    assert heartbeat["stop"] is False


def test_resume_clears_stop_flag_without_releasing_held_job(server, api, seed_clip, manifest):
    api.register("a")
    seed_clip(CLIP)
    _claim(api, "a")
    assert api.post("/workers/a/stop")[0] == 204

    status, _ = api.post("/workers/a/resume")
    assert status == 204
    status, heartbeat = api.post("/workers/a/heartbeat", {"active": ACTIVE})
    assert heartbeat["stop"] is False
    assert heartbeat["stop_mode"] is None
    assert manifest.list_clips()[0].state is JobState.CLAIMED


def test_stop_and_resume_unknown_worker_404(api):
    status, body = api.post("/workers/ghost/stop")
    assert status == 404 and body["error"]["code"] == "unknown_worker"
    status, body = api.post("/workers/ghost/resume")
    assert status == 404 and body["error"]["code"] == "unknown_worker"
