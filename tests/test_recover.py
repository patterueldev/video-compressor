"""Unit tests for crash recovery (core/recover.py)."""

from __future__ import annotations

import os
from pathlib import Path

from videocompressor.core.recover import (
    REVIEW_MARKER,
    RecoveryOutcome,
    reconcile_source,
)
from videocompressor.core.replace import (
    backup_path_for,
    find_artifacts,
    temp_path_for,
)

ORIGINAL = b"original bytes" * 64
REPLACEMENT = b"replacement bytes" * 80


def _confirm_replacement(path: Path) -> bool:
    return path.read_bytes() == REPLACEMENT


def _snapshot(directory: Path) -> dict[str, tuple[int, str]]:
    return {
        entry.name: (entry.stat().st_size, entry.read_bytes()[:16].hex())
        for entry in sorted(directory.iterdir())
        if entry.is_file()
    }


def test_clean_source_nothing_to_do(tmp_path: Path):
    source = tmp_path / "clip.MP4"
    source.write_bytes(ORIGINAL)

    report = reconcile_source(source, confirm=_confirm_replacement)

    assert report.outcome is RecoveryOutcome.NOTHING_TO_DO
    assert report.temps_removed == ()
    assert source.read_bytes() == ORIGINAL


def test_missing_source_without_artifacts(tmp_path: Path):
    source = tmp_path / "clip.MP4"

    report = reconcile_source(source, confirm=_confirm_replacement)

    assert report.outcome is RecoveryOutcome.SOURCE_MISSING
    assert "no swap artifacts" in (report.detail or "")


def test_temp_only_removed_and_original_untouched(tmp_path: Path):
    source = tmp_path / "clip.MP4"
    source.write_bytes(ORIGINAL)
    temp = temp_path_for(source, "crashed-job")
    temp.write_bytes(REPLACEMENT)

    report = reconcile_source(source, confirm=_confirm_replacement)

    assert report.outcome is RecoveryOutcome.TEMP_REMOVED
    assert report.temps_removed == (temp,)
    assert not temp.exists()
    assert source.read_bytes() == ORIGINAL

    before = _snapshot(tmp_path)
    second = reconcile_source(source, confirm=_confirm_replacement)
    assert second.outcome is RecoveryOutcome.NOTHING_TO_DO
    assert _snapshot(tmp_path) == before


def test_missing_source_with_temp_only_keeps_temp_for_review(tmp_path: Path):
    source = tmp_path / "clip.MP4"
    temp = temp_path_for(source, "crashed-job")
    temp.write_bytes(REPLACEMENT)

    report = reconcile_source(source, confirm=_confirm_replacement)

    assert report.outcome is RecoveryOutcome.SOURCE_MISSING
    assert report.review_paths == (temp,)
    assert temp.read_bytes() == REPLACEMENT
    assert not source.exists()


def test_backup_only_restores_original(tmp_path: Path):
    source = tmp_path / "clip.MP4"
    backup = backup_path_for(source, "crashed-job")
    backup.write_bytes(ORIGINAL)

    report = reconcile_source(source, confirm=_confirm_replacement)

    assert report.outcome is RecoveryOutcome.ORIGINAL_RESTORED
    assert report.restored_from == backup
    assert not backup.exists()
    assert source.read_bytes() == ORIGINAL

    before = _snapshot(tmp_path)
    second = reconcile_source(source, confirm=_confirm_replacement)
    assert second.outcome is RecoveryOutcome.NOTHING_TO_DO
    assert _snapshot(tmp_path) == before


def test_backup_and_temp_restores_original_and_removes_temps(tmp_path: Path):
    source = tmp_path / "clip.MP4"
    backup = backup_path_for(source, "crashed-job")
    backup.write_bytes(ORIGINAL)
    temp = temp_path_for(source, "crashed-job")
    temp.write_bytes(REPLACEMENT)

    report = reconcile_source(source, confirm=_confirm_replacement)

    assert report.outcome is RecoveryOutcome.ORIGINAL_RESTORED
    assert source.read_bytes() == ORIGINAL
    assert not backup.exists()
    assert report.temps_removed == (temp,)
    assert not find_artifacts(source)


def test_source_and_backup_readable_confirms_replacement(tmp_path: Path):
    source = tmp_path / "clip.MP4"
    source.write_bytes(REPLACEMENT)
    backup = backup_path_for(source, "crashed-job")
    backup.write_bytes(ORIGINAL)

    report = reconcile_source(source, confirm=_confirm_replacement)

    assert report.outcome is RecoveryOutcome.REPLACEMENT_CONFIRMED
    assert report.backups_removed == (backup,)
    assert not backup.exists()
    assert source.read_bytes() == REPLACEMENT

    before = _snapshot(tmp_path)
    second = reconcile_source(source, confirm=_confirm_replacement)
    assert second.outcome is RecoveryOutcome.NOTHING_TO_DO
    assert _snapshot(tmp_path) == before


def test_source_and_backup_unreadable_restores_original(tmp_path: Path):
    source = tmp_path / "clip.MP4"
    source.write_bytes(b"truncated garbage")
    backup = backup_path_for(source, "crashed-job")
    backup.write_bytes(ORIGINAL)

    report = reconcile_source(source, confirm=lambda path: False)

    assert report.outcome is RecoveryOutcome.ORIGINAL_RESTORED
    assert report.restored_from == backup
    assert source.read_bytes() == ORIGINAL
    assert not find_artifacts(source)


def test_probe_error_is_treated_as_unreadable(tmp_path: Path):
    source = tmp_path / "clip.MP4"
    source.write_bytes(REPLACEMENT)
    backup = backup_path_for(source, "crashed-job")
    backup.write_bytes(ORIGINAL)

    def exploding_probe(path: Path) -> bool:
        raise ValueError("probe crashed")

    report = reconcile_source(source, confirm=exploding_probe)

    assert report.outcome is RecoveryOutcome.ORIGINAL_RESTORED
    assert "probe error" in (report.detail or "")
    assert source.read_bytes() == ORIGINAL


def test_multiple_backups_missing_source_restores_newest_and_parks_rest(tmp_path: Path):
    source = tmp_path / "clip.MP4"
    older = backup_path_for(source, "job-old")
    newer = backup_path_for(source, "job-new")
    older.write_bytes(b"oldest original")
    newer.write_bytes(ORIGINAL)
    os.utime(older, ns=(1_000_000, 1_000_000))
    os.utime(newer, ns=(2_000_000, 2_000_000))
    temp = temp_path_for(source, "job-new")
    temp.write_bytes(REPLACEMENT)

    report = reconcile_source(source, confirm=_confirm_replacement)

    assert report.outcome is RecoveryOutcome.NEEDS_REVIEW
    assert report.restored_from == newer
    assert source.read_bytes() == ORIGINAL
    parked = source.with_name(f"clip.MP4{REVIEW_MARKER}job-old")
    assert parked in report.review_paths
    assert parked.read_bytes() == b"oldest original"
    assert temp not in report.review_paths
    assert not temp.exists()
    assert not find_artifacts(source)

    before = _snapshot(tmp_path)
    second = reconcile_source(source, confirm=_confirm_replacement)
    assert second.outcome is RecoveryOutcome.NOTHING_TO_DO
    assert _snapshot(tmp_path) == before


def test_multiple_backups_with_source_present_reports_review_without_touching(
    tmp_path: Path,
):
    source = tmp_path / "clip.MP4"
    source.write_bytes(REPLACEMENT)
    first = backup_path_for(source, "job-1")
    second = backup_path_for(source, "job-2")
    first.write_bytes(ORIGINAL)
    second.write_bytes(ORIGINAL)
    temp = temp_path_for(source, "job-2")
    temp.write_bytes(REPLACEMENT)

    report = reconcile_source(source, confirm=_confirm_replacement)

    assert report.outcome is RecoveryOutcome.NEEDS_REVIEW
    assert first.exists() and second.exists()
    assert source.read_bytes() == REPLACEMENT
    assert not temp.exists()

    before = _snapshot(tmp_path)
    repeat = reconcile_source(source, confirm=_confirm_replacement)
    assert repeat.outcome is RecoveryOutcome.NEEDS_REVIEW
    assert _snapshot(tmp_path) == before


def test_other_clips_artifacts_are_untouched(tmp_path: Path):
    source = tmp_path / "clip.MP4"
    source.write_bytes(ORIGINAL)
    temp = temp_path_for(source, "crashed-job")
    temp.write_bytes(REPLACEMENT)
    other = tmp_path / "other.MP4"
    other_temp = temp_path_for(other, "other-job")
    other_backup = backup_path_for(other, "other-job")
    other_temp.write_bytes(b"other temp")
    other_backup.write_bytes(b"other backup")

    reconcile_source(source, confirm=_confirm_replacement)

    assert other_temp.read_bytes() == b"other temp"
    assert other_backup.read_bytes() == b"other backup"


def test_review_files_are_not_artifacts(tmp_path: Path):
    source = tmp_path / "clip.MP4"
    source.write_bytes(ORIGINAL)
    parked = source.with_name(f"clip.MP4{REVIEW_MARKER}job-1")
    parked.write_bytes(b"parked backup")

    report = reconcile_source(source, confirm=_confirm_replacement)

    assert report.outcome is RecoveryOutcome.NOTHING_TO_DO
    assert parked.read_bytes() == b"parked backup"
