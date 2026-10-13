"""Root-aware claiming: ``claim_sql`` and the SQLite manifest filter (issue #83)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest

from videocompressor.core import manifest as manifest_module
from videocompressor.core.manifest import Manifest
from videocompressor.core.manifest_schema import CLAIM_SQL, claim_sql
from videocompressor.core.models import ClipRecord, Fingerprint, JobState


def _clip(root_id: str, relative_path: str, created_at: datetime) -> ClipRecord:
    return ClipRecord(
        root_id=root_id,
        relative_path=relative_path,
        source=Fingerprint(size_bytes=1, mtime_ns=1, digest="digest"),
        created_at=created_at,
        updated_at=created_at,
    )


@pytest.fixture()
def sqlite_manifest(tmp_path: Path, clock, monkeypatch) -> Iterator[Manifest]:
    monkeypatch.setattr(manifest_module, "_utcnow", clock)
    handle = Manifest.open(tmp_path / "manifest.sqlite")
    yield handle
    handle.close()


def test_claim_sql_is_unfiltered_for_none():
    assert claim_sql(None) == (CLAIM_SQL, ["queued"])


def test_claim_sql_matches_nothing_for_an_empty_set():
    sql, params = claim_sql(frozenset())
    assert params == ["queued"]
    assert "IN (NULL)" in sql


def test_claim_sql_builds_a_deterministic_in_list():
    sql, params = claim_sql(frozenset({"camera", "archive"}))
    assert params == ["queued", "archive", "camera"]
    assert sql.count("?") == 3
    assert "IN (?, ?)" in sql


def test_claim_next_filters_to_a_mapped_root(sqlite_manifest, clock):
    sqlite_manifest.upsert_clip(_clip("archive", "a.MP4", clock()))
    clock.advance(1)
    sqlite_manifest.upsert_clip(_clip("camera", "b.MP4", clock()))

    claimed = sqlite_manifest.claim_next("w1", roots=frozenset({"camera"}))
    assert claimed is not None
    assert claimed.relative_path == "b.MP4"
    assert claimed.state is JobState.CLAIMED and claimed.worker_id == "w1"

    unmapped = sqlite_manifest.get_clip("archive", "a.MP4")
    assert unmapped is not None
    assert unmapped.state is JobState.QUEUED and unmapped.worker_id is None


def test_claim_next_with_no_mappings_claims_nothing(sqlite_manifest, clock):
    sqlite_manifest.upsert_clip(_clip("archive", "a.MP4", clock()))

    assert sqlite_manifest.claim_next("w1", roots=frozenset()) is None
    record = sqlite_manifest.get_clip("archive", "a.MP4")
    assert record is not None and record.state is JobState.QUEUED


def test_claim_next_default_stays_unfiltered(sqlite_manifest, clock):
    sqlite_manifest.upsert_clip(_clip("archive", "a.MP4", clock()))

    claimed = sqlite_manifest.claim_next("w1")
    assert claimed is not None and claimed.relative_path == "a.MP4"
