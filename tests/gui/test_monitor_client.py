"""HTTP monitor client against a real controller (issue #9)."""

from __future__ import annotations

import socket

import pytest

from fixtures.controller import TOKEN
from videocompressor.controller.protocol import ControllerError
from videocompressor.controller.server import ControllerServer
from videocompressor.core.models import ArchiveRoot, JobState
from videocompressor.gui.monitor_client import (
    HttpControllerClient,
    MonitorUnavailable,
    describe_monitor_error,
)


def _client(server, token: str | None = TOKEN) -> HttpControllerClient:
    return HttpControllerClient(server.base_url, token)


def test_workers_events_and_summary_round_trip(server, api, seed_clip):
    api.register("homeserver", name="Linux homeserver")
    seed_clip("done.MP4", state=JobState.COMPLETE)
    client = _client(server)

    workers = client.workers()
    assert workers["workers"][0]["worker_id"] == "homeserver"
    assert workers["workers"][0]["online"] is True

    events = client.events()
    assert [event["message"] for event in events["events"]] == [
        "worker homeserver online",
        "worker homeserver registered",
    ]
    later = client.events(after=events["events"][0]["seq"])
    assert all(event["seq"] > events["events"][0]["seq"] for event in later["events"])
    assert len(client.events(limit=1)["events"]) == 1

    summary = client.summary()
    assert summary["completed"] == 1
    assert summary["run_id"] is None


def test_summary_for_a_finished_run(server):
    run = server.start_run()
    finished = server.finish_run()
    body = _client(server).summary(run_id=finished["run_id"])
    assert body["run_id"] == run["run_id"]
    assert (body["completed"], body["skipped"], body["failed"], body["review"]) == (0, 0, 0, 0)


def test_stop_and_resume_round_trip(server, api, seed_clip):
    api.register("a")
    seed_clip("clip.MP4")
    assert api.post("/jobs/claim", {"worker_id": "a"})[0] == 200
    client = _client(server)

    client.stop_worker("a")
    entry = client.workers()["workers"][0]
    assert entry["stop_requested"] is True
    assert entry["stop_mode"] == "cancel"

    client.resume_worker("a")
    entry = client.workers()["workers"][0]
    assert entry["stop_requested"] is False
    assert entry["stop_mode"] is None

    client.stop_worker("a", mode="drain")
    entry = client.workers()["workers"][0]
    assert entry["stop_requested"] is True
    assert entry["stop_mode"] == "drain"


def test_enqueue_round_trip(tmp_path, store, manifest, clock):
    archive = tmp_path / "archive"
    output = tmp_path / "output"
    archive.mkdir()
    output.mkdir()
    (archive / "clip.MP4").write_bytes(b"clip")
    roots = [
        ArchiveRoot(id="archive", name="Archive", path=archive),
        ArchiveRoot(id="compressed", name="Output", path=output),
    ]
    controller = ControllerServer(
        store=store,
        manifest=manifest,
        token=TOKEN,
        port=0,
        clock=clock,
        roots=lambda: roots,
        sweep_interval_seconds=3600,
    )
    controller.start_background()
    try:
        client = HttpControllerClient(controller.base_url, TOKEN)
        payload = {
            "files": [{"root_id": "archive", "relative_path": "clip.MP4"}],
            "destination": {
                "kind": "output_root",
                "root_id": "compressed",
                "relative_path": "2026-10",
            },
            "template_id": "motorcycle",
            "delete_original": False,
        }

        body = client.enqueue(payload)

        assert (body["queued"], body["skipped"], body["rejected"]) == (1, 0, 0)
        assert body["jobs"][0]["status"] == "queued"

        again = client.enqueue(payload)
        assert again["skipped"] == 1
        assert again["jobs"][0]["reason"] == "already_queued"

        refused = {**payload, "destination": {"kind": "same_folder"}, "delete_original": False}
        with pytest.raises(ControllerError) as excinfo:
            client.enqueue(refused)
        assert (excinfo.value.status, excinfo.value.code) == (400, "bad_request")
    finally:
        controller.stop()


def test_unknown_worker_and_auth_errors_map_to_controller_error(server):
    with pytest.raises(ControllerError) as excinfo:
        _client(server).stop_worker("ghost")
    assert (excinfo.value.status, excinfo.value.code) == (404, "unknown_worker")

    with pytest.raises(ControllerError) as excinfo:
        HttpControllerClient(server.base_url, "wrong-token").probe()
    assert excinfo.value.status == 401


def test_unreachable_controller_raises_monitor_unavailable():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    client = HttpControllerClient(f"http://127.0.0.1:{port}", "token", timeout=0.5)
    with pytest.raises(MonitorUnavailable) as excinfo:
        client.workers()
    assert "unreachable" in str(excinfo.value)


def test_describe_monitor_error():
    assert (
        describe_monitor_error(ControllerError(404, "unknown_worker", "worker 'x' is unknown"))
        == "worker 'x' is unknown"
    )
    assert (
        describe_monitor_error(MonitorUnavailable("controller unreachable at http://x"))
        == "controller unreachable at http://x"
    )
    assert describe_monitor_error(ValueError("boom")) == "unexpected error: ValueError('boom')"
