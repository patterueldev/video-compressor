"""The claim race guarantee, root-aware claim routing, and effective settings resolution."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from functools import partial
from zoneinfo import ZoneInfo

from videocompressor.controller.memory_manifest import MemoryManifest
from videocompressor.controller.server import ControllerServer
from videocompressor.controller.store import ControllerStore
from videocompressor.core.models import (
    CaptureTime,
    CaptureTimeMode,
    ClipRecord,
    Fingerprint,
    JobState,
    TemplateSettings,
)


def _run_threads(*targets: Callable[[], None]) -> None:
    """Run targets on threads; re-raise the first unhandled thread failure.

    Thread failures must fail the test deterministically (issue #24): a
    crashed claim worker can otherwise hide behind pytest's warning-as-error
    flag, or be masked when other workers cover its clips.
    """
    errors: list[BaseException] = []
    guard = threading.Lock()

    def run(target: Callable[[], None]) -> None:
        try:
            target()
        except BaseException as exc:
            with guard:
                errors.append(exc)

    threads = [threading.Thread(target=run, args=(target,)) for target in targets]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if errors:
        raise errors[0]


def test_concurrent_http_claims_exactly_one_winner(server, api, seed_clip, manifest):
    api.register("a")
    api.register("b")
    seed_clip("only.MP4")

    barrier = threading.Barrier(2)
    results: dict[str, tuple[int, dict | None]] = {}

    def claim(worker_id: str) -> None:
        barrier.wait()
        results[worker_id] = api.post("/jobs/claim", {"worker_id": worker_id})

    _run_threads(partial(claim, "a"), partial(claim, "b"))

    assert sorted(status for status, _ in results.values()) == [200, 204]
    winner = next(worker for worker, (status, _) in results.items() if status == 200)
    loser_body = results["b" if winner == "a" else "a"][1]
    assert loser_body is None
    assert results[winner][1]["job"]["relative_path"] == "only.MP4"

    record = manifest.list_clips()[0]
    assert record.state is JobState.CLAIMED
    assert record.worker_id == winner


def test_many_workers_race_many_clips_exactly_once_each(server, api, seed_clip):
    expected = [f"clip-{index}.MP4" for index in range(6)]
    for relative_path in expected:
        seed_clip(relative_path)
    for index in range(6):
        api.register(f"w{index}")

    collected: list[str] = []
    guard = threading.Lock()
    barrier = threading.Barrier(6)

    def process(worker_id: str) -> None:
        barrier.wait()
        while True:
            status, body = api.post("/jobs/claim", {"worker_id": worker_id})
            if status == 204:
                return
            assert status == 200, body
            with guard:
                collected.append(body["job"]["relative_path"])

    _run_threads(*(partial(process, f"w{index}") for index in range(6)))

    assert sorted(collected) == sorted(expected)
    assert len(collected) == len(set(collected)) == len(expected)


def test_claim_carries_the_template_video_bitrate(tmp_path, clock, make_api):
    templates = {"motorcycle": replace(TemplateSettings(), video_bitrate_kbps=700)}
    manifest = MemoryManifest(clock=clock)
    store = ControllerStore.open(tmp_path / "controller.sqlite", clock=clock)
    source = Fingerprint(size_bytes=4096, mtime_ns=1, digest="source-digest")
    manila = ZoneInfo("Asia/Manila")

    def clip(relative_path: str, when: datetime | None) -> ClipRecord:
        return ClipRecord(
            root_id="archive",
            relative_path=relative_path,
            source=source,
            template_id="motorcycle",
            capture_time=CaptureTime(when=when),
            created_at=clock(),
            updated_at=clock(),
        )

    with ControllerServer(
        store=store,
        manifest=manifest,
        token="t",
        port=0,
        clock=clock,
        templates=templates,
        sweep_interval_seconds=3600,
    ) as controller:
        client = make_api(controller, token="t")
        client.register("w")
        manifest.upsert_clip(clip("evening.MP4", datetime(2024, 6, 14, 17, 41, tzinfo=manila)))
        manifest.upsert_clip(clip("day.MP4", datetime(2024, 6, 14, 10, 0, tzinfo=manila)))
        manifest.upsert_clip(clip("naive.MP4", datetime(2024, 6, 14, 18, 0)))

        status, evening = client.post("/jobs/claim", {"worker_id": "w"})
        assert status == 200
        assert evening["job"]["settings"]["video_bitrate_kbps"] == 700
        assert evening["job"]["capture_time"] == "2024-06-14T17:41:00+08:00"

        status, day = client.post("/jobs/claim", {"worker_id": "w"})
        assert status == 200
        assert day["job"]["settings"]["video_bitrate_kbps"] == 700

        # a timestamp without an offset is local wall-clock time; the bitrate is not per clip
        status, naive = client.post("/jobs/claim", {"worker_id": "w"})
        assert status == 200
        assert naive["job"]["settings"]["video_bitrate_kbps"] == 700


def test_claim_routes_clips_by_the_workers_roots(api, seed_clip, manifest):
    api.register("node-a", roots={"archive": "/mnt/archive"})
    api.register("node-b", roots={"folders": "/srv/folders"})
    seed_clip("archive.MP4", root_id="archive")
    seed_clip("folder.MP4", root_id="folders")

    status, body = api.post("/jobs/claim", {"worker_id": "node-a"})
    assert status == 200
    assert body["job"]["root_id"] == "archive"
    assert body["job"]["relative_path"] == "archive.MP4"

    status, body = api.post("/jobs/claim", {"worker_id": "node-b"})
    assert status == 200
    assert body["job"]["root_id"] == "folders"
    assert body["job"]["relative_path"] == "folder.MP4"

    for root_id, relative_path, worker_id in (
        ("archive", "archive.MP4", "node-a"),
        ("folders", "folder.MP4", "node-b"),
    ):
        record = manifest.get_clip(root_id, relative_path)
        assert record is not None and record.state is JobState.CLAIMED
        assert record.worker_id == worker_id
    assert api.post("/jobs/claim", {"worker_id": "node-a"}) == (204, None)


def test_unmapped_root_clip_stays_queued_and_never_failed(api, seed_clip, manifest):
    api.register("node-a", roots={"archive": "/mnt/archive"})
    api.register("node-b", roots={"folders": "/srv/folders"})
    seed_clip("orphan.MP4", root_id="camera")

    assert api.post("/jobs/claim", {"worker_id": "node-a"}) == (204, None)
    assert api.post("/jobs/claim", {"worker_id": "node-b"}) == (204, None)

    record = manifest.get_clip("camera", "orphan.MP4")
    assert record is not None
    assert record.state is JobState.QUEUED
    assert record.worker_id is None
    assert record.error is None

    status, events = api.get("/events")
    assert status == 200
    assert all(event.get("relative_path") != "orphan.MP4" for event in events["events"])


def test_filter_skips_an_unmapped_older_clip(api, seed_clip, clock):
    api.register("node-a", roots={"archive": "/mnt/archive"})
    seed_clip("orphan.MP4", root_id="camera")  # older, but no worker maps it
    clock.advance(1)
    seed_clip("mapped.MP4", root_id="archive")

    status, body = api.post("/jobs/claim", {"worker_id": "node-a"})
    assert status == 200
    assert body["job"]["relative_path"] == "mapped.MP4"


def test_worker_without_mappings_claims_nothing(api, seed_clip, manifest):
    api.register("bare", roots={})
    seed_clip("clip.MP4")

    assert api.post("/jobs/claim", {"worker_id": "bare"}) == (204, None)
    record = manifest.get_clip("archive", "clip.MP4")
    assert record is not None and record.state is JobState.QUEUED


def test_reregistration_refreshes_the_claim_filter(api, seed_clip, manifest):
    api.register("node-a", roots={"archive": "/mnt/archive"})
    seed_clip("first.MP4", root_id="archive")
    status, body = api.post("/jobs/claim", {"worker_id": "node-a"})
    assert status == 200 and body["job"]["relative_path"] == "first.MP4"

    api.register("node-a", roots={"folders": "/srv/folders"})
    seed_clip("second.MP4", root_id="folders")
    seed_clip("third.MP4", root_id="archive")

    status, body = api.post("/jobs/claim", {"worker_id": "node-a"})
    assert status == 200 and body["job"]["relative_path"] == "second.MP4"
    assert api.post("/jobs/claim", {"worker_id": "node-a"}) == (204, None)
    record = manifest.get_clip("archive", "third.MP4")
    assert record is not None and record.state is JobState.QUEUED


# -- output name patterns (issue #87) -----------------------------------------


def _naming_claim(
    tmp_path, clock, make_api, templates, *, capture_mode=CaptureTimeMode.LOCAL_WALL_CLOCK
):
    """Claim one seeded clip from a controller with ``templates`` and return its job."""
    manifest = MemoryManifest(clock=clock)
    store = ControllerStore.open(tmp_path / "controller.sqlite", clock=clock)
    source = Fingerprint(size_bytes=4096, mtime_ns=1, digest="source-digest")
    manifest.upsert_clip(
        ClipRecord(
            root_id="archive",
            relative_path="clip.MP4",
            source=source,
            template_id="motorcycle",
            created_at=clock(),
            updated_at=clock(),
        )
    )
    with ControllerServer(
        store=store,
        manifest=manifest,
        token="t",
        port=0,
        clock=clock,
        templates=templates,
        capture_mode=capture_mode,
        sweep_interval_seconds=3600,
    ) as controller:
        client = make_api(controller, token="t")
        client.register("w")
        status, body = client.post("/jobs/claim", {"worker_id": "w"})
    assert status == 200, body
    return body["job"]


def test_claim_omits_naming_for_the_default_pattern(api, seed_clip):
    api.register("w")
    seed_clip("clip.MP4")

    status, body = api.post("/jobs/claim", {"worker_id": "w"})

    assert status == 200
    assert "naming" not in body["job"]


def test_claim_carries_naming_for_a_non_default_pattern(tmp_path, clock, make_api):
    templates = {"motorcycle": replace(TemplateSettings(), name_pattern="{date}_{name}")}

    job = _naming_claim(tmp_path, clock, make_api, templates)

    assert job["naming"] == {
        "pattern": "{date}_{name}",
        "timezone": "Asia/Manila",
        "capture_mode": "local_wall_clock",
    }


def test_claim_naming_carries_the_configured_capture_mode(tmp_path, clock, make_api):
    templates = {"motorcycle": replace(TemplateSettings(), name_pattern="{date}_{name}")}

    job = _naming_claim(tmp_path, clock, make_api, templates, capture_mode=CaptureTimeMode.UTC)

    assert job["naming"]["capture_mode"] == "utc"
    assert job["naming"]["timezone"] == "Asia/Manila"
