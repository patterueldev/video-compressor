"""Explicit-selection enqueue: jobs, dedup, statuses, and mapping (issue #39)."""

from __future__ import annotations

from pathlib import Path

import pytest

from fixtures.controller import TOKEN, ApiClient
from videocompressor.controller.memory_manifest import MemoryManifest
from videocompressor.controller.server import ControllerServer
from videocompressor.core.fingerprint import fingerprint_file
from videocompressor.core.models import (
    ArchiveRoot,
    ClipRecord,
    Destination,
    DestinationKind,
    Fingerprint,
    JobState,
)

CLIP = "2024-06-Ride/GS010001.MP4"
OTHER = "IMPORTS/GOPRO-20261010/GH011016.MP4"
DESTINATION = {"kind": "output_root", "root_id": "compressed", "relative_path": "2026-10"}
SAME_FOLDER = {"kind": "same_folder"}


def file_entry(relative_path: str, *, root_id: str = "archive") -> dict[str, str]:
    return {"root_id": root_id, "relative_path": relative_path}


def write_clip(root: ArchiveRoot, relative_path: str, payload: bytes = b"clip") -> Path:
    path = root.path / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


@pytest.fixture
def roots(tmp_path: Path) -> list[ArchiveRoot]:
    archive = tmp_path / "archive"
    output = tmp_path / "output"
    archive.mkdir()
    output.mkdir()
    return [
        ArchiveRoot(id="archive", name="Archive", path=archive),
        ArchiveRoot(id="compressed", name="Output", path=output),
    ]


@pytest.fixture
def manifest(clock) -> MemoryManifest:
    return MemoryManifest(clock=clock)


@pytest.fixture
def server(store, manifest, clock, roots):
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
    yield controller
    controller.stop()


@pytest.fixture
def api(server) -> ApiClient:
    return ApiClient(server)


def test_batch_enqueue_creates_leasable_jobs(api, manifest, roots):
    archive = roots[0]
    write_clip(archive, CLIP)
    write_clip(archive, OTHER)

    status, body = api.post(
        "/jobs",
        {
            "files": [file_entry(CLIP), file_entry(OTHER)],
            "destination": DESTINATION,
            "delete_original": True,
        },
    )
    assert status == 200
    assert [job["job_id"] for job in body["jobs"]] == [f"archive:{CLIP}", f"archive:{OTHER}"]
    assert [job["status"] for job in body["jobs"]] == ["queued", "queued"]
    assert [job["reason"] for job in body["jobs"]] == [None, None]
    assert (body["queued"], body["skipped"], body["rejected"]) == (2, 0, 0)

    record = manifest.get_clip("archive", CLIP)
    assert record is not None
    assert record.state is JobState.QUEUED
    assert record.template_id == "motorcycle"
    assert record.destination == Destination(DestinationKind.OUTPUT_ROOT, "compressed", "2026-10")
    assert record.delete_original is True
    assert record.source == fingerprint_file(archive.path / CLIP)

    api.register("worker-a")
    status, claimed = api.post("/jobs/claim", {"worker_id": "worker-a"})
    assert status == 200
    assert claimed["job"]["relative_path"] == CLIP
    assert claimed["job"]["destination"] == DESTINATION
    assert claimed["job"]["delete_original"] is True
    status, _ = api.post(
        "/jobs/complete",
        {
            "worker_id": "worker-a",
            "root_id": "archive",
            "relative_path": CLIP,
            "output": {"size_bytes": 10, "digest": "out"},
        },
    )
    assert status == 204


def test_re_enqueue_is_idempotent(api, manifest, roots):
    write_clip(roots[0], CLIP)
    payload = {"files": [file_entry(CLIP)], "destination": SAME_FOLDER, "delete_original": True}
    assert api.post("/jobs", payload)[0] == 200

    status, body = api.post("/jobs", payload)
    assert status == 200
    assert body["jobs"][0] == {
        "job_id": f"archive:{CLIP}",
        "root_id": "archive",
        "relative_path": CLIP,
        "status": "skipped",
        "reason": "already_queued",
    }
    assert (body["queued"], body["skipped"], body["rejected"]) == (0, 1, 0)
    assert len(manifest.list_clips()) == 1


def test_duplicate_in_batch_queues_once(api, manifest, roots):
    write_clip(roots[0], CLIP)
    status, body = api.post(
        "/jobs",
        {"files": [file_entry(CLIP), file_entry(CLIP)], "destination": SAME_FOLDER,
         "delete_original": True},
    )
    assert status == 200
    assert [job["status"] for job in body["jobs"]] == ["queued", "skipped"]
    assert body["jobs"][1]["reason"] == "duplicate_in_batch"
    assert len(manifest.list_clips()) == 1


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        (JobState.COMPLETE, "already_complete"),
        (JobState.REVIEW, "needs_review"),
        (JobState.FAILED, "previously_failed"),
        (JobState.CLAIMED, "already_queued"),
        (JobState.QUEUED, "already_queued"),
    ],
)
def test_existing_records_skip_with_a_reason(api, manifest, roots, state, reason):
    write_clip(roots[0], CLIP)
    manifest.upsert_clip(
        ClipRecord(
            root_id="archive",
            relative_path=CLIP,
            source=Fingerprint(size_bytes=1, mtime_ns=2, digest="recorded"),
            state=state,
            error="prior outcome",
        )
    )

    status, body = api.post("/jobs", {"files": [file_entry(CLIP)], "destination": DESTINATION})
    assert status == 200
    assert body["jobs"][0]["status"] == "skipped"
    assert body["jobs"][0]["reason"] == reason
    assert body["queued"] == 0
    kept = manifest.get_clip("archive", CLIP)
    assert kept is not None and kept.state is state and kept.error == "prior outcome"


def test_rejections_are_per_file_and_do_not_block(api, manifest, roots):
    archive = roots[0]
    write_clip(archive, CLIP)
    (archive.path / "a-dir").mkdir()

    status, body = api.post(
        "/jobs",
        {
            "files": [
                file_entry(CLIP),
                file_entry(CLIP, root_id="unknown"),
                file_entry("../escape.MP4"),
                file_entry("missing.MP4"),
                file_entry("a-dir"),
            ],
            "destination": DESTINATION,
        },
    )
    assert status == 200
    assert [(job["status"], job["reason"]) for job in body["jobs"]] == [
        ("queued", None),
        ("rejected", "unknown_root"),
        ("rejected", "unsafe_relative_path"),
        ("rejected", "not_a_file"),
        ("rejected", "not_a_file"),
    ]
    assert (body["queued"], body["skipped"], body["rejected"]) == (1, 0, 4)
    assert len(manifest.list_clips()) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"files": []},
        {"files": "clip.MP4", "destination": SAME_FOLDER},
        {"files": [CLIP], "destination": SAME_FOLDER},
        {"files": [{"root_id": "archive"}], "destination": SAME_FOLDER},
        {"files": [{"root_id": "archive", "relative_path": ""}], "destination": SAME_FOLDER},
        {"files": [file_entry(CLIP)]},
        {"files": [file_entry(CLIP)], "destination": None},
        {"files": [file_entry(CLIP)], "destination": {"kind": "elsewhere"}},
        {"files": [file_entry(CLIP)], "destination": {"kind": "output_root", "root_id": "nope"}},
        {
            "files": [file_entry(CLIP)],
            "destination": {
                "kind": "output_root",
                "root_id": "compressed",
                "relative_path": "../escape",
            },
        },
        {"files": [file_entry(CLIP)], "destination": DESTINATION, "template_id": "missing"},
        {"files": [file_entry(CLIP)], "destination": DESTINATION, "delete_original": "yes"},
        # a same-folder output replaces the source, so it needs delete_original=true
        {"files": [file_entry(CLIP)], "destination": SAME_FOLDER},
        {"files": [file_entry(CLIP)], "destination": SAME_FOLDER, "delete_original": False},
    ],
)
def test_malformed_batches_are_rejected_without_writing(api, manifest, roots, payload):
    write_clip(roots[0], CLIP)
    status, body = api.post("/jobs", payload)
    assert status == 400
    assert body["error"]["code"] == "bad_request"
    assert manifest.list_clips() == []


def test_defaults_apply_when_optional_fields_are_omitted(api, manifest, roots):
    write_clip(roots[0], CLIP)
    status, _ = api.post("/jobs", {"files": [file_entry(CLIP)], "destination": DESTINATION})
    assert status == 200
    record = manifest.get_clip("archive", CLIP)
    assert record is not None
    assert record.template_id == "motorcycle"
    assert record.delete_original is False
    assert record.destination == Destination(DestinationKind.OUTPUT_ROOT, "compressed", "2026-10")

    api.register("worker-a")
    status, claimed = api.post("/jobs/claim", {"worker_id": "worker-a"})
    assert status == 200
    assert claimed["job"]["destination"] == DESTINATION
    assert claimed["job"]["delete_original"] is False


def test_output_root_without_a_folder_mirrors_the_source_path(api, manifest, roots):
    write_clip(roots[0], CLIP)
    status, _ = api.post(
        "/jobs",
        {
            "files": [file_entry(CLIP)],
            "destination": {"kind": "output_root", "root_id": "compressed"},
        },
    )
    assert status == 200
    record = manifest.get_clip("archive", CLIP)
    assert record is not None
    assert record.destination == Destination(DestinationKind.OUTPUT_ROOT, "compressed", "")

    api.register("worker-a")
    status, claimed = api.post("/jobs/claim", {"worker_id": "worker-a"})
    assert status == 200
    assert claimed["job"]["destination"] == {"kind": "output_root", "root_id": "compressed"}


def test_enqueue_requires_auth(api, roots):
    write_clip(roots[0], CLIP)
    status, body = api.post(
        "/jobs",
        {"files": [file_entry(CLIP)], "destination": SAME_FOLDER, "delete_original": True},
        token=None,
    )
    assert status == 401 and body["error"]["code"] == "unauthorized"


def test_get_jobs_is_method_not_allowed(api):
    status, body = api.get("/jobs")
    assert status == 405 and body["error"]["code"] == "method_not_allowed"


def test_enqueue_logs_one_run_event(api, server, roots):
    write_clip(roots[0], CLIP)
    api.post(
        "/jobs",
        {"files": [file_entry(CLIP)], "destination": SAME_FOLDER, "delete_original": True},
    )
    messages = [event["message"] for event in server.events()["events"]]
    assert [message for message in messages if "enqueued" in message] == [
        "enqueued 1 of 1 clips (0 skipped, 0 rejected)"
    ]
