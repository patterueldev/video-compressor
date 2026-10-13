"""Orphan grace: the job lease is held while the Monitor flips offline (issue #75)."""

from __future__ import annotations

import pytest

from fixtures.controller import TOKEN
from videocompressor.controller.memory_manifest import MemoryManifest
from videocompressor.controller.server import ControllerServer
from videocompressor.controller.store import ControllerStore
from videocompressor.core.models import ClipRecord, Fingerprint, JobState

CLIP = "2024-06-Ride/GS010001.MP4"
ACTIVE = {"root_id": "archive", "relative_path": CLIP}


def _seed(manifest, clock) -> None:
    manifest.upsert_clip(
        ClipRecord(
            root_id="archive",
            relative_path=CLIP,
            source=Fingerprint(size_bytes=4096, mtime_ns=1, digest="source-digest"),
            created_at=clock(),
            updated_at=clock(),
        )
    )


def _messages(store, substring: str) -> list[str]:
    return [event.message for event in store.list_events() if substring in event.message]


@pytest.fixture
def grace_server(tmp_path, clock):
    """Build a controller whose job lease is ``grace`` seconds long."""
    created: list[ControllerServer] = []

    def _make(grace: int) -> ControllerServer:
        controller = ControllerServer(
            store=ControllerStore.open(tmp_path / f"store-{grace}.sqlite", clock=clock),
            manifest=MemoryManifest(clock=clock, lease_ttl_seconds=grace),
            token=TOKEN,
            port=0,
            clock=clock,
            lease_ttl_seconds=grace,
            sweep_interval_seconds=3600,
        )
        controller.start_background()
        created.append(controller)
        return controller

    yield _make
    for controller in created:
        controller.stop()


def test_long_grace_holds_the_job_while_the_monitor_flips_offline(grace_server, make_api, clock):
    controller = grace_server(300)
    api = make_api(controller)
    api.register("laptop")
    _seed(controller.manifest, clock)
    assert api.post("/jobs/claim", {"worker_id": "laptop"})[0] == 200

    clock.advance(91)  # past the 90 s online window, inside the 300 s grace
    controller.sweep_once()

    assert _messages(controller.store, "worker laptop offline") == ["worker laptop offline"]
    assert _messages(controller.store, "lease expired") == []
    record = controller.manifest.get_clip("archive", CLIP)
    assert record is not None and record.state is JobState.CLAIMED
    workers = api.get("/workers")[1]["workers"]
    assert workers[0]["online"] is False
    api.register("other")
    status, body = api.post("/jobs/claim", {"worker_id": "other"})
    assert status == 204 and body is None

    # the node wakes inside the grace: the heartbeat renews and it resumes
    clock.advance(29)  # t+120
    status, _ = api.post("/workers/laptop/heartbeat", {"active": ACTIVE})
    assert status == 200
    workers = api.get("/workers")[1]["workers"]
    laptop = next(worker for worker in workers if worker["worker_id"] == "laptop")
    assert laptop["online"] is True

    clock.advance(301)  # t+421: past the renewed t+420 lease
    controller.sweep_once()

    assert _messages(controller.store, "lease expired") == [f"requeued {CLIP}: lease expired"]
    record = controller.manifest.get_clip("archive", CLIP)
    assert record is not None and record.state is JobState.QUEUED
    status, claimed = api.post("/jobs/claim", {"worker_id": "other"})
    assert status == 200 and claimed["job"]["relative_path"] == CLIP


def test_short_grace_flips_offline_and_requeues_in_one_sweep(grace_server, make_api, clock):
    controller = grace_server(30)
    api = make_api(controller)
    api.register("laptop")
    _seed(controller.manifest, clock)
    assert api.post("/jobs/claim", {"worker_id": "laptop"})[0] == 200

    clock.advance(31)  # past min(30, 90) = 30
    controller.sweep_once()

    assert _messages(controller.store, "worker laptop offline") == ["worker laptop offline"]
    assert _messages(controller.store, "lease expired") == [f"requeued {CLIP}: lease expired"]
    record = controller.manifest.get_clip("archive", CLIP)
    assert record is not None and record.state is JobState.QUEUED
    assert record.worker_id is None
