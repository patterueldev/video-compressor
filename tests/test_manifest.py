"""SQLite manifest: records, transactional claims, leases, recovery, run log."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from videocompressor.core import manifest as manifest_module
from videocompressor.core.manifest import (
    HEARTBEAT_INTERVAL_SECONDS,
    LEASE_TTL_SECONDS,
    LeaseError,
    Manifest,
)
from videocompressor.core.models import (
    CaptureTime,
    CaptureTimeMode,
    Classification,
    ClipRecord,
    Fingerprint,
    JobState,
)

START = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def _fingerprint(seed: int = 0) -> Fingerprint:
    return Fingerprint(size_bytes=100 + seed, mtime_ns=1_000_000_000 + seed, digest=f"{seed:064x}")


def _record(
    relative_path: str,
    *,
    state: JobState = JobState.QUEUED,
    created_at: datetime = START,
    updated_at: datetime | None = None,
    **changes: object,
) -> ClipRecord:
    return ClipRecord(
        root_id="archive",
        relative_path=relative_path,
        source=_fingerprint(len(relative_path)),
        state=state,
        created_at=created_at,
        updated_at=updated_at if updated_at is not None else created_at,
        **changes,  # type: ignore[arg-type]
    )


class _Clock:
    def __init__(self, now: datetime = START) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture()
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    clock = _Clock()
    monkeypatch.setattr(manifest_module, "_utcnow", clock)
    return clock


@pytest.fixture()
def manifest(tmp_path: Path) -> Manifest:
    handle = Manifest.open(tmp_path / "manifest.sqlite")
    yield handle
    handle.close()


def test_open_creates_database_and_roundtrips_a_record(manifest: Manifest):
    capture = CaptureTime(
        when=datetime(2024, 6, 14, 17, 41, tzinfo=timezone(timedelta(hours=8))),
        raw="2024-06-14T09:41:00Z",
        source="format",
        mode=CaptureTimeMode.UTC,
    )
    record = _record(
        "2024-06-Ride/GS010001.MP4",
        classification=Classification.COMPLETE,
        capture_time=capture,
        template_id="motorcycle",
        settings_hash="abc123",
        worker_id="homeserver",
        output=_fingerprint(7),
        completed_at=START,
    )

    manifest.upsert_clip(record)

    assert manifest.get_clip("archive", "2024-06-Ride/GS010001.MP4") == record
    assert manifest.list_clips() == [record]
    assert manifest.list_clips(state=JobState.QUEUED) == [record]
    assert manifest.list_clips(state=JobState.COMPLETE) == []


def test_upsert_updates_an_existing_record(manifest: Manifest):
    manifest.upsert_clip(_record("clip.MP4"))
    updated = _record(
        "clip.MP4",
        state=JobState.REVIEW,
        created_at=START + timedelta(days=1),
        updated_at=START + timedelta(days=1),
        error="capture time conflicts",
    )

    manifest.upsert_clip(updated)

    stored = manifest.get_clip("archive", "clip.MP4")
    assert stored is not None
    assert stored.state is JobState.REVIEW
    assert stored.error == "capture time conflicts"
    assert stored.created_at == START  # the first insert keeps its creation time
    assert len(manifest.list_clips()) == 1


def test_list_clips_is_ordered_by_creation(manifest: Manifest):
    manifest.upsert_clip(_record("b.MP4", created_at=START + timedelta(minutes=1)))
    manifest.upsert_clip(_record("a.MP4", created_at=START))
    manifest.upsert_clip(_record("c.MP4", created_at=START + timedelta(minutes=2)))

    assert [record.relative_path for record in manifest.list_clips()] == [
        "a.MP4",
        "b.MP4",
        "c.MP4",
    ]


def test_claim_next_is_fifo_and_never_double_claims(manifest: Manifest, clock: _Clock):
    manifest.upsert_clip(_record("first.MP4", created_at=START))
    manifest.upsert_clip(_record("second.MP4", created_at=START + timedelta(seconds=1)))

    claim = manifest.claim_next("worker-a")

    assert claim is not None
    assert claim.relative_path == "first.MP4"
    assert claim.state is JobState.CLAIMED
    assert claim.worker_id == "worker-a"

    other = manifest.claim_next("worker-b")
    assert other is not None
    assert other.relative_path == "second.MP4"

    assert manifest.claim_next("worker-c") is None


def test_expired_lease_requeues_and_clears_the_worker(manifest: Manifest, clock: _Clock):
    manifest.upsert_clip(_record("clip.MP4"))
    claimed = manifest.claim_next("worker-a")
    assert claimed is not None

    clock.advance(LEASE_TTL_SECONDS + 1)
    assert manifest.reconcile() == []

    recovered = manifest.get_clip("archive", "clip.MP4")
    assert recovered is not None
    assert recovered.state is JobState.QUEUED
    assert recovered.worker_id is None
    assert recovered.error is not None
    assert manifest.claim_next("worker-b") is not None


def test_live_lease_survives_reconcile(manifest: Manifest, clock: _Clock):
    manifest.upsert_clip(_record("clip.MP4"))
    assert manifest.claim_next("worker-a") is not None

    clock.advance(HEARTBEAT_INTERVAL_SECONDS * 4)
    manifest.heartbeat("archive", "clip.MP4", "worker-a")
    clock.advance(LEASE_TTL_SECONDS - HEARTBEAT_INTERVAL_SECONDS)

    assert manifest.reconcile() == []
    stored = manifest.get_clip("archive", "clip.MP4")
    assert stored is not None
    assert stored.state is JobState.CLAIMED


def test_heartbeat_without_the_lease_raises(manifest: Manifest, clock: _Clock):
    manifest.upsert_clip(_record("clip.MP4"))
    manifest.claim_next("worker-a")

    with pytest.raises(LeaseError):
        manifest.heartbeat("archive", "clip.MP4", "worker-b")
    with pytest.raises(LeaseError):
        manifest.heartbeat("archive", "unknown.MP4", "worker-a")


@pytest.mark.parametrize(
    "state",
    [JobState.ENCODING, JobState.COPIED, JobState.CLAIMED],
)
def test_reconcile_requeues_unstarted_stages(manifest: Manifest, clock: _Clock, state: JobState):
    manifest.upsert_clip(_record("clip.MP4"))
    manifest.claim_next("worker-a")
    if state is not JobState.CLAIMED:
        manifest.update_state("archive", "clip.MP4", state)

    clock.advance(LEASE_TTL_SECONDS + 1)

    assert manifest.reconcile() == []
    stored = manifest.get_clip("archive", "clip.MP4")
    assert stored is not None
    assert stored.state is JobState.QUEUED


@pytest.mark.parametrize(
    "state",
    [JobState.VALIDATED, JobState.TRANSFERRING, JobState.REPLACING],
)
def test_reconcile_parks_replacement_stages_in_review(
    manifest: Manifest, clock: _Clock, state: JobState
):
    manifest.upsert_clip(_record("clip.MP4"))
    manifest.claim_next("worker-a")
    manifest.update_state("archive", "clip.MP4", state)

    clock.advance(LEASE_TTL_SECONDS + 1)
    recovered = manifest.reconcile()

    assert [record.relative_path for record in recovered] == ["clip.MP4"]
    assert recovered[0].state is JobState.REVIEW
    assert recovered[0].error is not None and "reconcile" in recovered[0].error
    # A clip in review cannot be claimed again.
    assert manifest.claim_next("worker-b") is None


def test_update_state_validates_input(manifest: Manifest):
    manifest.upsert_clip(_record("clip.MP4"))

    with pytest.raises(KeyError):
        manifest.update_state("archive", "unknown.MP4", JobState.QUEUED)
    with pytest.raises(ValueError, match="unsupported clip fields"):
        manifest.update_state("archive", "clip.MP4", JobState.QUEUED, bogus=1)


def test_update_state_to_complete_stamps_time_and_releases_lease(manifest: Manifest, clock: _Clock):
    manifest.upsert_clip(_record("clip.MP4"))
    manifest.claim_next("worker-a")

    manifest.update_state("archive", "clip.MP4", JobState.COMPLETE, output=_fingerprint(9))

    stored = manifest.get_clip("archive", "clip.MP4")
    assert stored is not None
    assert stored.state is JobState.COMPLETE
    assert stored.completed_at == clock.now
    assert stored.output == _fingerprint(9)

    clock.advance(LEASE_TTL_SECONDS * 10)
    assert manifest.reconcile() == []
    assert manifest.get_clip("archive", "clip.MP4").state is JobState.COMPLETE  # type: ignore[union-attr]


def test_update_state_requeue_clears_worker_and_lease(manifest: Manifest):
    manifest.upsert_clip(_record("clip.MP4"))
    manifest.claim_next("worker-a")

    manifest.update_state("archive", "clip.MP4", JobState.QUEUED)

    stored = manifest.get_clip("archive", "clip.MP4")
    assert stored is not None
    assert stored.worker_id is None
    with pytest.raises(LeaseError):
        manifest.heartbeat("archive", "clip.MP4", "worker-a")


def test_run_log_appends_and_lists_events(manifest: Manifest):
    first = manifest.record_event("info", "scan started", root_id="archive")
    second = manifest.record_event(
        "info", "claimed clip.MP4", root_id="archive", relative_path="clip.MP4"
    )
    third = manifest.record_event(
        "warning", "source changed", root_id="archive", relative_path="clip.MP4"
    )

    assert [first.seq, second.seq, third.seq] == [1, 2, 3]
    assert manifest.list_events() == [first, second, third]
    assert manifest.list_events(after=1) == [second, third]
    assert manifest.list_events(limit=2) == [second, third]


def test_read_only_open_never_writes(tmp_path: Path):
    db_path = tmp_path / "manifest.sqlite"
    with Manifest.open(db_path) as writable:
        writable.upsert_clip(_record("clip.MP4"))

    with Manifest.open(db_path, create=False) as readonly:
        assert readonly.get_clip("archive", "clip.MP4") is not None
        with pytest.raises(sqlite3.OperationalError):
            readonly.upsert_clip(_record("other.MP4"))

    with pytest.raises(sqlite3.OperationalError):
        Manifest.open(tmp_path / "missing.sqlite", create=False)


def test_lease_constants_match_the_architecture_contract():
    assert LEASE_TTL_SECONDS == 90
    assert HEARTBEAT_INTERVAL_SECONDS == 15
