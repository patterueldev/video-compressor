"""MemoryManifest: claiming, leases, state updates, and expiry reconciliation."""

from __future__ import annotations

import threading

import pytest

from videocompressor.controller.constants import LEASE_TTL_SECONDS
from videocompressor.controller.memory_manifest import MemoryManifest
from videocompressor.core.models import JobState


def test_claim_is_fifo_and_assigns_worker(manifest, seed_clip):
    seed_clip("a.MP4")
    seed_clip("b.MP4")
    first = manifest.claim_next("w1")
    second = manifest.claim_next("w2")
    assert first is not None and first.relative_path == "a.MP4" and first.worker_id == "w1"
    assert second is not None and second.relative_path == "b.MP4" and second.worker_id == "w2"
    assert second.state is JobState.CLAIMED
    assert manifest.claim_next("w3") is None


def test_claim_next_skips_non_queued(manifest, seed_clip):
    seed_clip("done.MP4", state=JobState.COMPLETE)
    assert manifest.claim_next("w") is None


def test_heartbeat_renews_lease(manifest, seed_clip, clock):
    seed_clip("a.MP4")
    manifest.claim_next("w1")  # lease until t+90
    clock.advance(80)
    manifest.heartbeat("archive", "a.MP4", "w1")  # renewed to t+170
    clock.advance(80)  # t+160: expired without the renewal
    assert manifest.reconcile() == []
    assert manifest.list_clips()[0].worker_id == "w1"


def test_expired_lease_is_requeued(manifest, seed_clip, clock):
    seed_clip("a.MP4")
    manifest.claim_next("w1")
    clock.advance(LEASE_TTL_SECONDS + 1)
    recovered = manifest.reconcile()
    assert [record.relative_path for record in recovered] == ["a.MP4"]
    assert recovered[0].state is JobState.QUEUED
    assert recovered[0].worker_id is None
    claimed = manifest.claim_next("w2")
    assert claimed is not None and claimed.worker_id == "w2"
    assert manifest.reconcile() == []  # nothing left to recover


def test_expired_during_replacing_parks_in_review(manifest, seed_clip, clock):
    seed_clip("a.MP4")
    manifest.claim_next("w1")
    manifest.update_state("archive", "a.MP4", JobState.REPLACING)
    clock.advance(LEASE_TTL_SECONDS + 1)
    recovered = manifest.reconcile()
    assert recovered[0].state is JobState.REVIEW
    assert "recovery required" in (recovered[0].error or "")
    assert manifest.claim_next("w2") is None


def test_update_state_clears_lease_and_rejects_unknown_fields(manifest, seed_clip, clock):
    seed_clip("a.MP4")
    manifest.claim_next("w1")
    manifest.update_state("archive", "a.MP4", JobState.COMPLETE, error=None)
    clock.advance(LEASE_TTL_SECONDS + 1)
    assert manifest.reconcile() == []
    with pytest.raises(TypeError):
        manifest.update_state("archive", "a.MP4", JobState.QUEUED, nonsense=1)
    with pytest.raises(KeyError):
        manifest.update_state("archive", "missing.MP4", JobState.QUEUED)


def test_concurrent_claims_have_exactly_one_winner(manifest, seed_clip):
    seed_clip("a.MP4")
    results = []
    barrier = threading.Barrier(4)

    def worker(worker_id: str) -> None:
        barrier.wait()
        results.append(manifest.claim_next(worker_id))

    threads = [threading.Thread(target=worker, args=(f"w{index}",)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    winners = [record for record in results if record is not None]
    assert len(winners) == 1
    assert winners[0].worker_id is not None


def test_open_returns_empty_manifest(tmp_path):
    opened = MemoryManifest.open(tmp_path / "ignored.sqlite")
    assert opened.list_clips() == []
