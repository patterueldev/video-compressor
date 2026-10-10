"""Folder markers: tolerant reads, atomic writes, merge-on-complete."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from videocompressor.core.markers import (
    MARKER_FILENAME,
    FolderMarker,
    MarkerClip,
    mark_completed,
    marker_path,
    read_marker,
    write_marker,
)
from videocompressor.core.models import Fingerprint


def _fingerprint(seed: int = 0) -> Fingerprint:
    return Fingerprint(size_bytes=100 + seed, mtime_ns=1_000_000_000 + seed, digest=f"{seed:064x}")


def test_read_marker_returns_none_when_absent_or_not_a_folder(tmp_path: Path):
    assert read_marker(tmp_path) is None
    assert read_marker(tmp_path / "missing") is None


def test_marker_roundtrips(tmp_path: Path):
    completed = datetime(2026, 10, 1, 9, 30, tzinfo=UTC)
    marker = FolderMarker(
        template_id="motorcycle",
        settings_hash="abc123",
        generated_at=completed,
        clips={"GS010001.MP4": MarkerClip(output=_fingerprint(1), completed_at=completed)},
    )

    write_marker(tmp_path, marker)

    assert read_marker(tmp_path) == marker
    assert marker_path(tmp_path).name == MARKER_FILENAME
    assert json.loads(marker_path(tmp_path).read_text())["version"] == 1
    # The atomic write leaves no temporary files behind.
    assert sorted(path.name for path in tmp_path.iterdir()) == [MARKER_FILENAME]


def test_read_marker_tolerates_corrupt_and_unknown_files(tmp_path: Path):
    write_target = marker_path(tmp_path)
    write_target.write_text("{not json")
    assert read_marker(tmp_path) is None

    write_target.write_text(json.dumps({"version": 99, "clips": {}}))
    assert read_marker(tmp_path) is None

    write_target.write_text(json.dumps({"version": 1, "clips": "nonsense"}))
    assert read_marker(tmp_path) == FolderMarker(clips={})


def test_read_marker_skips_malformed_clip_entries(tmp_path: Path):
    marker_path(tmp_path).write_text(
        json.dumps(
            {
                "version": 1,
                "clips": {
                    "good.MP4": {
                        "output": {"size_bytes": 5, "mtime_ns": 6, "digest": "abcd"},
                    },
                    "broken.MP4": {"output": {"size_bytes": "NaN"}},
                    "also-broken.MOV": 7,
                },
            }
        )
    )

    marker = read_marker(tmp_path)

    assert marker is not None
    assert set(marker.clips) == {"good.MP4"}
    assert marker.clips["good.MP4"].output == Fingerprint(size_bytes=5, mtime_ns=6, digest="abcd")


def test_mark_completed_merges_and_refreshes_template(tmp_path: Path):
    first = mark_completed(
        tmp_path,
        filename="a.MP4",
        output=_fingerprint(1),
        template_id="motorcycle",
        settings_hash="old",
        completed_at=datetime(2026, 10, 1, tzinfo=UTC),
    )
    second = mark_completed(
        tmp_path,
        filename="b.MOV",
        output=_fingerprint(2),
        template_id="motorcycle",
        settings_hash="new",
    )

    assert set(first.clips) == {"a.MP4"}
    assert set(second.clips) == {"a.MP4", "b.MOV"}
    assert second.settings_hash == "new"
    stored = read_marker(tmp_path)
    assert stored is not None
    assert set(stored.clips) == {"a.MP4", "b.MOV"}
    assert stored.clips["b.MOV"].completed_at is not None
