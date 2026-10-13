"""Controller against the real SQLite manifest (#3) — the production seam.

The rest of the controller suite injects ``MemoryManifest``; these tests run the
HTTP server, store, and sweep against ``core.manifest.Manifest`` so the two
implementations cannot drift.
"""

from __future__ import annotations

import pytest

from fixtures.controller import ApiClient
from videocompressor.controller.server import ControllerServer, open_manifest
from videocompressor.controller.store import ControllerStore
from videocompressor.core import manifest as manifest_module
from videocompressor.core.manifest import LEASE_TTL_SECONDS, Manifest
from videocompressor.core.models import (
    ArchiveRoot,
    ClipRecord,
    Destination,
    DestinationKind,
    Fingerprint,
    JobState,
)

TOKEN = "sqlite-token"
CLIP = "2024-06-Ride/GS010001.MP4"
ACTIVE = {"root_id": "archive", "relative_path": CLIP}


def _seed(manifest: Manifest, relative_path: str = CLIP, **fields: object) -> None:
    manifest.upsert_clip(
        ClipRecord(
            root_id="archive",
            relative_path=relative_path,
            source=Fingerprint(size_bytes=4096, mtime_ns=1, digest="source-digest"),
            **fields,  # type: ignore[arg-type]
        )
    )


@pytest.fixture
def sqlite_controller(tmp_path, clock, monkeypatch):
    monkeypatch.setattr(manifest_module, "_utcnow", clock)
    manifest = Manifest.open(tmp_path / "manifest.sqlite")
    store = ControllerStore.open(tmp_path / "controller.sqlite", clock=clock)
    controller = ControllerServer(
        store=store,
        manifest=manifest,
        token=TOKEN,
        port=0,
        clock=clock,
        sweep_interval_seconds=3600,
    )
    controller.start_background()
    yield manifest, controller
    controller.stop()
    manifest.close()


def test_full_flow_on_the_sqlite_manifest(sqlite_controller, make_api):
    manifest, controller = sqlite_controller
    api = make_api(controller, token=TOKEN)
    api.register("homeserver")
    _seed(manifest)

    status, body = api.post("/jobs/claim", {"worker_id": "homeserver"})
    assert status == 200
    job = body["job"]
    assert job["relative_path"] == CLIP
    assert job["template_id"] == "motorcycle"
    record = manifest.get_clip("archive", CLIP)
    assert record is not None and record.state is JobState.CLAIMED

    status, heartbeat = api.post(
        "/workers/homeserver/heartbeat",
        {"active": ACTIVE, "progress": {"phase": "encoding", "percent": 10.5}},
    )
    assert status == 200 and heartbeat["stop"] is False
    record = manifest.get_clip("archive", CLIP)
    assert record is not None and record.state is JobState.ENCODING

    status, _ = api.post(
        "/jobs/complete",
        {
            "worker_id": "homeserver",
            "root_id": "archive",
            "relative_path": CLIP,
            "output": {"size_bytes": 100, "mtime_ns": 5, "digest": "out"},
        },
    )
    assert status == 204
    record = manifest.get_clip("archive", CLIP)
    assert record is not None and record.state is JobState.COMPLETE
    assert record.output is not None and record.output.digest == "out"


def test_expired_lease_requeues_through_the_sqlite_manifest(sqlite_controller, make_api, clock):
    manifest, controller = sqlite_controller
    api = make_api(controller, token=TOKEN)
    api.register("homeserver")
    _seed(manifest)
    assert api.post("/jobs/claim", {"worker_id": "homeserver"})[0] == 200

    clock.advance(LEASE_TTL_SECONDS + 1)
    controller.sweep_once()

    record = manifest.get_clip("archive", CLIP)
    assert record is not None and record.state is JobState.QUEUED
    assert record.worker_id is None
    events = controller.store.list_events()
    assert any(
        "requeued" in event.message and event.worker_id == "homeserver" for event in events
    )

    api.register("other")
    status, body = api.post("/jobs/claim", {"worker_id": "other"})
    assert status == 200 and body["job"]["relative_path"] == CLIP


def test_expired_during_replacing_parks_review_on_sqlite(sqlite_controller, make_api, clock):
    manifest, controller = sqlite_controller
    api = make_api(controller, token=TOKEN)
    api.register("homeserver")
    _seed(manifest)
    assert api.post("/jobs/claim", {"worker_id": "homeserver"})[0] == 200
    manifest.update_state("archive", CLIP, JobState.REPLACING)

    clock.advance(LEASE_TTL_SECONDS + 1)
    controller.sweep_once()

    record = manifest.get_clip("archive", CLIP)
    assert record is not None and record.state is JobState.REVIEW
    assert "reconcile the source" in (record.error or "")
    events = controller.store.list_events()
    parked = [event for event in events if "parked in review" in event.message]
    assert parked and parked[0].worker_id == "homeserver"

    api.register("other")
    status, body = api.post("/jobs/claim", {"worker_id": "other"})
    assert status == 204 and body is None


def test_open_manifest_prefers_the_sqlite_manifest(tmp_path, clock, monkeypatch):
    monkeypatch.setattr(manifest_module, "_utcnow", clock)
    manifest, durable = open_manifest(tmp_path / "manifest.sqlite", clock=clock)
    try:
        assert durable is True
        _seed(manifest)
        claimed = manifest.claim_next("w")
        assert claimed is not None and claimed.state is JobState.CLAIMED
    finally:
        manifest.close()


def test_enqueue_survives_a_controller_restart(tmp_path, clock, monkeypatch):
    monkeypatch.setattr(manifest_module, "_utcnow", clock)
    archive = tmp_path / "archive"
    output = tmp_path / "output"
    archive.mkdir()
    output.mkdir()
    (archive / CLIP).parent.mkdir(parents=True)
    (archive / CLIP).write_bytes(b"clip")
    roots = [
        ArchiveRoot(id="archive", name="Archive", path=archive),
        ArchiveRoot(id="compressed", name="Output", path=output),
    ]
    manifest_path = tmp_path / "manifest.sqlite"
    store_path = tmp_path / "controller.sqlite"

    manifest = Manifest.open(manifest_path)
    controller = ControllerServer(
        store=ControllerStore.open(store_path, clock=clock),
        manifest=manifest,
        token=TOKEN,
        port=0,
        clock=clock,
        roots=lambda: roots,
        sweep_interval_seconds=3600,
    )
    controller.start_background()
    try:
        api = ApiClient(controller, token=TOKEN)
        status, body = api.post(
            "/jobs",
            {
                "files": [{"root_id": "archive", "relative_path": CLIP}],
                "destination": {
                    "kind": "output_root",
                    "root_id": "compressed",
                    "relative_path": "2026-10",
                },
                "delete_original": True,
            },
        )
        assert status == 200 and body["queued"] == 1
    finally:
        controller.stop()  # closes the controller store
    manifest.close()

    reopened = Manifest.open(manifest_path)
    try:
        record = reopened.get_clip("archive", CLIP)
        assert record is not None and record.state is JobState.QUEUED
        assert record.destination == Destination(
            DestinationKind.OUTPUT_ROOT, "compressed", "2026-10"
        )
        assert record.delete_original is True

        controller = ControllerServer(
            store=ControllerStore.open(store_path, clock=clock),
            manifest=reopened,
            token=TOKEN,
            port=0,
            clock=clock,
            roots=lambda: roots,
            sweep_interval_seconds=3600,
        )
        controller.start_background()
        try:
            api = ApiClient(controller, token=TOKEN)
            api.register("node-a")
            status, claimed = api.post("/jobs/claim", {"worker_id": "node-a"})
            assert status == 200
            assert claimed["job"]["destination"]["root_id"] == "compressed"
            assert claimed["job"]["delete_original"] is True
        finally:
            controller.stop()
    finally:
        reopened.close()


def test_open_manifest_forwards_the_custom_lease_ttl(tmp_path, clock, monkeypatch):
    monkeypatch.setattr(manifest_module, "_utcnow", clock)
    manifest, durable = open_manifest(
        tmp_path / "manifest.sqlite", clock=clock, lease_ttl_seconds=300
    )
    try:
        assert durable is True
        _seed(manifest)
        assert manifest.claim_next("a") is not None

        clock.advance(200)  # past the 90 s fallback, inside the configured 300 s
        assert manifest.reconcile() == []
        record = manifest.get_clip("archive", CLIP)
        assert record is not None and record.state is JobState.CLAIMED
    finally:
        manifest.close()


def test_custom_lease_ttl_drives_claim_and_heartbeat(tmp_path, clock, monkeypatch):
    monkeypatch.setattr(manifest_module, "_utcnow", clock)
    manifest = Manifest.open(tmp_path / "manifest.sqlite", lease_ttl_seconds=300)
    try:
        _seed(manifest)
        assert manifest.claim_next("a") is not None

        clock.advance(200)
        assert manifest.reconcile() == []
        manifest.heartbeat("archive", CLIP, "a")  # renews under the custom TTL

        clock.advance(299)  # t+499: still inside the renewed lease
        assert manifest.reconcile() == []
        record = manifest.get_clip("archive", CLIP)
        assert record is not None and record.state is JobState.CLAIMED

        clock.advance(2)  # t+501: silent for longer than the custom TTL
        manifest.reconcile()
        record = manifest.get_clip("archive", CLIP)
        assert record is not None and record.state is JobState.QUEUED
    finally:
        manifest.close()
