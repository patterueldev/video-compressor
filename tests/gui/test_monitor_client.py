"""HTTP monitor client against a real controller (issue #9)."""

from __future__ import annotations

import socket

import pytest

from fixtures.controller import TOKEN
from videocompressor.controller.protocol import ControllerError
from videocompressor.core.models import JobState
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
    assert client.workers()["workers"][0]["stop_requested"] is True

    client.resume_worker("a")
    assert client.workers()["workers"][0]["stop_requested"] is False


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
