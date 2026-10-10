"""Controller client: contract round trips and typed failures (issue #6)."""

from __future__ import annotations

import pytest

from fixtures.controller import TOKEN
from videocompressor.core.models import Fingerprint, JobState
from videocompressor.worker.client import (
    ClaimedJob,
    ControllerAuthError,
    ControllerClient,
    ControllerError,
    ControllerUnavailable,
    LeaseConflict,
)

CLIP = "2024-06-Ride/GS010001.MP4"


def register(client: ControllerClient, worker_id: str = "node-a") -> dict:
    return client.register(
        worker_id=worker_id,
        name=worker_id,
        roots={"archive": "/srv/archive"},
        scratch_dir="/srv/scratch",
        thread_cap=2,
        version="0.1.0",
    )


def test_register_heartbeat_claim_complete_round_trip(server, seed_clip, manifest):
    seed_clip(CLIP)
    client = ControllerClient(server.base_url, TOKEN)

    assert register(client) == {"lease_seconds": 90, "heartbeat_seconds": 15}
    assert client.heartbeat("node-a") == {"stop": False, "lease_seconds": 90}

    job = client.claim("node-a")
    assert isinstance(job, ClaimedJob)
    assert (job.root_id, job.relative_path) == ("archive", CLIP)
    assert job.template_id == "motorcycle"
    assert job.settings["output_height"] == 720
    assert job.settings_hash

    client.complete("node-a", job, Fingerprint(size_bytes=10, mtime_ns=1, digest="out"))
    record = manifest.get_clip("archive", CLIP)
    assert record.state is JobState.COMPLETE
    assert record.output == Fingerprint(size_bytes=10, mtime_ns=1, digest="out")


def test_claim_returns_none_when_queue_is_empty(server):
    client = ControllerClient(server.base_url, TOKEN)
    register(client)
    assert client.claim("node-a") is None


def test_fail_reports_review(server, seed_clip, manifest):
    seed_clip(CLIP)
    client = ControllerClient(server.base_url, TOKEN)
    register(client)
    job = client.claim("node-a")
    assert job is not None

    client.fail("node-a", job, error="source changed", disposition="review")

    record = manifest.get_clip("archive", CLIP)
    assert record.state is JobState.REVIEW
    assert record.error == "source changed"


def test_wrong_token_is_an_auth_error(server):
    client = ControllerClient(server.base_url, "wrong-token")
    with pytest.raises(ControllerAuthError) as excinfo:
        register(client)
    assert excinfo.value.status == 401
    assert excinfo.value.code == "unauthorized"


def test_unknown_worker_is_a_controller_error(server):
    client = ControllerClient(server.base_url, TOKEN)
    with pytest.raises(ControllerError) as excinfo:
        client.heartbeat("ghost")
    assert excinfo.value.status == 404
    assert excinfo.value.code == "unknown_worker"


def test_stale_lease_is_a_lease_conflict(server, api, seed_clip):
    api.register("a")
    api.register("b")
    seed_clip(CLIP)
    assert api.post("/jobs/claim", {"worker_id": "a"})[0] == 200

    client = ControllerClient(server.base_url, TOKEN)
    with pytest.raises(LeaseConflict):
        client.heartbeat("b", active=("archive", CLIP))


def test_controller_unavailable_when_nothing_listens():
    client = ControllerClient("http://127.0.0.1:1", TOKEN, timeout_s=1.0)
    with pytest.raises(ControllerUnavailable):
        client.claim("node-a")
