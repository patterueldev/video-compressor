"""The claim race guarantee and effective settings resolution."""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

from videocompressor.controller.memory_manifest import MemoryManifest
from videocompressor.controller.server import ControllerServer
from videocompressor.controller.store import ControllerStore
from videocompressor.core.models import (
    CaptureTime,
    ClipRecord,
    Fingerprint,
    JobState,
    TemplateSettings,
)


def test_concurrent_http_claims_exactly_one_winner(server, api, seed_clip, manifest):
    api.register("a")
    api.register("b")
    seed_clip("only.MP4")

    barrier = threading.Barrier(2)
    results: dict[str, tuple[int, dict | None]] = {}

    def claim(worker_id: str) -> None:
        barrier.wait()
        results[worker_id] = api.post("/jobs/claim", {"worker_id": worker_id})

    threads = [threading.Thread(target=claim, args=(worker_id,)) for worker_id in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

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

    threads = [threading.Thread(target=process, args=(f"w{index}",)) for index in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(collected) == sorted(expected)
    assert len(collected) == len(set(collected)) == len(expected)


def test_claim_resolves_day_and_evening_bitrates(tmp_path, clock, make_api):
    templates = {
        "motorcycle": replace(
            TemplateSettings(),
            daytime_video_bitrate_kbps=700,
            evening_video_bitrate_kbps=1500,
        )
    }
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
        assert evening["job"]["settings"]["video_bitrate_kbps"] == 1500
        assert evening["job"]["capture_time"] == "2024-06-14T17:41:00+08:00"

        status, day = client.post("/jobs/claim", {"worker_id": "w"})
        assert status == 200
        assert day["job"]["settings"]["video_bitrate_kbps"] == 700

        # a timestamp without an offset is local wall-clock time
        status, naive = client.post("/jobs/claim", {"worker_id": "w"})
        assert status == 200
        assert naive["job"]["settings"]["video_bitrate_kbps"] == 1500
