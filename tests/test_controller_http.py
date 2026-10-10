"""HTTP contract: auth, payload validation, registry, events, and summary."""

from __future__ import annotations

import pytest

from videocompressor.controller.server import ControllerError
from videocompressor.core.models import Classification, Fingerprint, JobState


def test_missing_and_invalid_tokens_are_rejected(api):
    status, body = api.post("/workers/register", {"worker_id": "a"}, token=None)
    assert status == 401
    assert body == {"error": {"code": "unauthorized", "message": "missing or invalid bearer token"}}
    status, body = api.get("/workers", token="wrong")
    assert status == 401
    assert body["error"]["code"] == "unauthorized"
    # auth is checked before routing
    status, _ = api.get("/nope", token=None)
    assert status == 401


def test_register_and_heartbeat_round_trip(api):
    body = api.register("homeserver", name="Linux homeserver")
    assert body == {"lease_seconds": 90, "heartbeat_seconds": 15}
    status, heartbeat = api.post("/workers/homeserver/heartbeat", {})
    assert status == 200
    assert heartbeat == {"stop": False, "lease_seconds": 90}
    status, workers = api.get("/workers")
    assert status == 200
    entry = workers["workers"][0]
    assert entry["worker_id"] == "homeserver"
    assert entry["name"] == "Linux homeserver"
    assert entry["online"] is True
    assert entry["roots"] == {"archive": "/srv/archive"}
    assert entry["active"] is None
    assert entry["stop_requested"] is False


def test_heartbeat_unknown_worker_404(api):
    status, body = api.post("/workers/ghost/heartbeat", {})
    assert status == 404
    assert body["error"]["code"] == "unknown_worker"


def test_malformed_payloads_rejected(api):
    status, body = api.post("/workers/register", body=b"{not json")
    assert status == 400 and body["error"]["code"] == "bad_request"
    status, body = api.post("/workers/register", {})
    assert status == 400
    status, body = api.post("/workers/register", {"worker_id": "bad worker!"})
    assert status == 400 and body["error"]["code"] == "invalid_worker_id"
    status, body = api.post("/workers/register", {"worker_id": "a", "roots": {"x": 1}})
    assert status == 400
    status, body = api.post("/workers/register", {"worker_id": "a", "thread_cap": "two"})
    assert status == 400
    status, body = api.post("/jobs/claim", {"worker_id": 7})
    assert status == 400


def test_unknown_route_and_method(api):
    status, body = api.get("/nope")
    assert status == 404 and body["error"]["code"] == "not_found"
    status, body = api.get("/jobs/claim")
    assert status == 405 and body["error"]["code"] == "method_not_allowed"
    status, body = api.post("/workers", {})
    assert status == 405 and body["error"]["code"] == "method_not_allowed"


def test_unknown_job_and_stale_lease_errors(api, seed_clip):
    api.register("a")
    api.register("b")
    status, body = api.post(
        "/jobs/complete",
        {
            "worker_id": "a",
            "root_id": "archive",
            "relative_path": "missing.MP4",
            "output": {"size_bytes": 1, "digest": "x"},
        },
    )
    assert status == 404 and body["error"]["code"] == "unknown_job"

    seed_clip("clip.MP4")
    status, _ = api.post("/jobs/claim", {"worker_id": "a"})
    assert status == 200
    status, body = api.post(
        "/jobs/complete",
        {
            "worker_id": "b",
            "root_id": "archive",
            "relative_path": "clip.MP4",
            "output": {"size_bytes": 1, "digest": "x"},
        },
    )
    assert status == 409 and body["error"]["code"] == "lease_conflict"


def test_claim_by_unknown_worker_404(api):
    status, body = api.post("/jobs/claim", {"worker_id": "ghost"})
    assert status == 404 and body["error"]["code"] == "unknown_worker"


def test_invalid_fail_disposition_and_output(api, seed_clip):
    api.register("a")
    seed_clip("clip.MP4")
    assert api.post("/jobs/claim", {"worker_id": "a"})[0] == 200
    base = {"worker_id": "a", "root_id": "archive", "relative_path": "clip.MP4"}
    status, body = api.post("/jobs/fail", {**base, "disposition": "shrug"})
    assert status == 400
    status, body = api.post("/jobs/complete", {**base, "output": "wrong"})
    assert status == 400
    status, body = api.post("/jobs/complete", {**base, "output": {"size_bytes": 1}})
    assert status == 400


def test_claim_heartbeat_complete_flow(api, seed_clip, manifest):
    api.register("a")
    seed_clip("clip.MP4")
    status, body = api.post("/jobs/claim", {"worker_id": "a"})
    assert status == 200
    job = body["job"]
    assert job["relative_path"] == "clip.MP4"
    assert job["template_id"] == "motorcycle"
    assert job["settings"] == {
        "output_height": 720,
        "video_bitrate_kbps": 800,
        "audio_bitrate_kbps": 96,
        "ffmpeg_preset": "medium",
    }
    assert len(job["settings_hash"]) == 64
    assert job["capture_time"] is None

    status, heartbeat = api.post(
        "/workers/a/heartbeat",
        {
            "active": {"root_id": "archive", "relative_path": "clip.MP4"},
            "progress": {"phase": "encoding", "pass": 2, "percent": 41.2},
        },
    )
    assert status == 200 and heartbeat["stop"] is False
    record = manifest.list_clips()[0]
    assert record.state is JobState.ENCODING
    assert record.settings_hash == job["settings_hash"]

    status, workers = api.get("/workers")
    assert workers["workers"][0]["progress"]["percent"] == 41.2
    assert workers["workers"][0]["active"]["relative_path"] == "clip.MP4"

    status, body = api.post(
        "/jobs/complete",
        {
            "worker_id": "a",
            "root_id": "archive",
            "relative_path": "clip.MP4",
            "output": {"size_bytes": 100, "mtime_ns": 5, "digest": "out"},
        },
    )
    assert status == 204 and body is None
    record = manifest.list_clips()[0]
    assert record.state is JobState.COMPLETE
    assert record.output == Fingerprint(size_bytes=100, mtime_ns=5, digest="out")


def test_events_endpoint_pagination(api):
    api.register("a")
    status, body = api.get("/events")
    assert status == 200
    events = body["events"]
    assert [event["message"] for event in events] == [
        "worker a online",
        "worker a registered",
    ]
    assert [event["seq"] for event in events] == sorted(event["seq"] for event in events)
    assert body["next"] == events[-1]["seq"] + 1

    status, later = api.get(f"/events?after={events[0]['seq']}")
    assert status == 200
    assert all(event["seq"] > events[0]["seq"] for event in later["events"])
    assert later["next"] == later["events"][-1]["seq"] + 1

    status, body = api.get("/events?after=abc")
    assert status == 400
    status, body = api.get("/events?after=999")
    assert status == 200 and body == {"events": [], "next": 999}


def test_summary_live_and_for_finished_run(server, api, seed_clip):
    seed_clip("done.MP4", state=JobState.COMPLETE)
    seed_clip("failed.MP4", state=JobState.FAILED)
    seed_clip("review.MP4", state=JobState.REVIEW)
    seed_clip("skipped.MP4", classification=Classification.COMPLETE)
    # a COMPLETE-classified clip keeps counting as skipped while it is active
    seed_clip("skipped-active.MP4", state=JobState.ENCODING, classification=Classification.COMPLETE)

    run = server.start_run()
    finished = server.finish_run()
    assert finished["run_id"] == run["run_id"]
    assert finished["completed"] == 1
    assert finished["skipped"] == 2
    assert finished["failed"] == 1
    assert finished["review"] == 1

    status, body = api.get(f"/summary?run_id={run['run_id']}")
    assert status == 200
    assert body["completed"] == 1 and body["skipped"] == 2
    assert body["run_id"] == run["run_id"]

    status, body = api.get("/summary")
    assert status == 200 and body["run_id"] is None
    assert (body["completed"], body["skipped"], body["failed"], body["review"]) == (1, 2, 1, 1)

    status, body = api.get("/summary?run_id=missing")
    assert status == 404 and body["error"]["code"] == "unknown_run"


def test_run_events_are_tagged_and_lifecycle_guard(server, api):
    run = server.start_run()
    with pytest.raises(ControllerError) as excinfo:
        server.start_run()
    assert excinfo.value.status == 409
    server.finish_run()
    with pytest.raises(ControllerError):
        server.finish_run()

    status, body = api.get("/events")
    tagged = [event for event in body["events"] if event.get("run_id") == run["run_id"]]
    assert [event["message"] for event in tagged] == [
        f"run {run['run_id']} started",
        f"run {run['run_id']} finished: 0 completed, 0 skipped, 0 failed, 0 review",
    ]
