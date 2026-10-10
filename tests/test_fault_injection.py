"""Fault-injection suite: simulate a crash at every step of the replacement protocol,
run recovery, and assert the original or its backup is always restored or accounted
for and that a second recovery run changes nothing."""

from __future__ import annotations

from pathlib import Path

import pytest

from videocompressor.core.recover import RecoveryOutcome, reconcile_source
from videocompressor.core.replace import (
    ArtifactsPresentError,
    find_artifacts,
    replace_with_verified_copy,
)

ORIGINAL = b"original GoPro bytes" * 256
REPLACEMENT = b"compressed replacement bytes" * 320


class InjectedCrash(RuntimeError):
    """Stands in for a process death at a protocol step."""


#: crash step -> (recovery outcome, bytes expected at the source afterwards)
CRASH_STEPS = [
    ("temp_written", RecoveryOutcome.TEMP_REMOVED, ORIGINAL),
    ("temp_verified", RecoveryOutcome.TEMP_REMOVED, ORIGINAL),
    ("source_verified", RecoveryOutcome.TEMP_REMOVED, ORIGINAL),
    ("backup_renamed", RecoveryOutcome.ORIGINAL_RESTORED, ORIGINAL),
    ("swapped", RecoveryOutcome.REPLACEMENT_CONFIRMED, REPLACEMENT),
    ("confirmed", RecoveryOutcome.REPLACEMENT_CONFIRMED, REPLACEMENT),
    ("backup_deleted", RecoveryOutcome.NOTHING_TO_DO, REPLACEMENT),
]

#: crash step -> ("original" | "replacement" | "missing", temps, backups)
CRASH_STATES = {
    "temp_written": ("original", 1, 0),
    "temp_verified": ("original", 1, 0),
    "source_verified": ("original", 1, 0),
    "backup_renamed": ("missing", 1, 1),
    "swapped": ("replacement", 0, 1),
    "confirmed": ("replacement", 0, 1),
    "backup_deleted": ("replacement", 0, 0),
}


def _confirm_replacement(path: Path) -> bool:
    return path.read_bytes() == REPLACEMENT


def _snapshot(directory: Path) -> dict[str, bytes]:
    return {
        entry.name: entry.read_bytes()
        for entry in sorted(directory.iterdir())
        if entry.is_file()
    }


def _prepare(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "GS010001.MP4"
    local_output = tmp_path / "scratch.MP4"
    source.write_bytes(ORIGINAL)
    local_output.write_bytes(REPLACEMENT)
    return source, local_output


def _run_until_crash(
    source: Path, local_output: Path, fingerprint_full, crash_step: str
) -> None:
    def crash_hook(step: str) -> None:
        if step == crash_step:
            raise InjectedCrash(step)

    with pytest.raises(InjectedCrash):
        replace_with_verified_copy(
            local_output,
            source,
            expected_source=fingerprint_full(source),
            job_id="job-1",
            fingerprint=fingerprint_full,
            confirm=_confirm_replacement,
            on_step=crash_hook,
        )


def _assert_crash_state(source: Path, crash_step: str) -> None:
    mode, temp_count, backup_count = CRASH_STATES[crash_step]
    artifacts = find_artifacts(source)
    assert len(artifacts.temps) == temp_count
    assert len(artifacts.backups) == backup_count

    if mode == "original":
        assert source.read_bytes() == ORIGINAL
    elif mode == "replacement":
        assert source.read_bytes() == REPLACEMENT
    else:
        assert not source.exists()

    for backup in artifacts.backups:
        assert backup.read_bytes() == ORIGINAL
    for temp in artifacts.temps:
        assert temp.read_bytes() == REPLACEMENT

    if crash_step != "backup_deleted":
        candidates = [source, *artifacts.backups]
        assert any(
            path.exists() and path.read_bytes() == ORIGINAL for path in candidates
        ), "the original must remain recoverable at the crash point"


@pytest.mark.parametrize(("crash_step", "expected_outcome", "final_bytes"), CRASH_STEPS)
def test_crash_at_each_step_then_recover(
    tmp_path: Path, fingerprint_full, crash_step, expected_outcome, final_bytes
):
    source, local_output = _prepare(tmp_path)
    _run_until_crash(source, local_output, fingerprint_full, crash_step)
    _assert_crash_state(source, crash_step)

    report = reconcile_source(source, confirm=_confirm_replacement)

    assert report.outcome is expected_outcome
    assert find_artifacts(source).all == ()
    assert source.read_bytes() == final_bytes

    before = _snapshot(tmp_path)
    second = reconcile_source(source, confirm=_confirm_replacement)
    assert second.outcome is RecoveryOutcome.NOTHING_TO_DO
    assert _snapshot(tmp_path) == before


def test_crash_after_swap_with_unreadable_replacement_restores_original(
    tmp_path: Path, fingerprint_full
):
    source, local_output = _prepare(tmp_path)
    _run_until_crash(source, local_output, fingerprint_full, "swapped")

    report = reconcile_source(source, confirm=lambda path: False)

    assert report.outcome is RecoveryOutcome.ORIGINAL_RESTORED
    assert source.read_bytes() == ORIGINAL
    assert find_artifacts(source).all == ()


def test_retry_is_blocked_until_recovery_runs(tmp_path: Path, fingerprint_full):
    source, local_output = _prepare(tmp_path)
    _run_until_crash(source, local_output, fingerprint_full, "temp_verified")

    with pytest.raises(ArtifactsPresentError):
        replace_with_verified_copy(
            local_output,
            source,
            expected_source=fingerprint_full(source),
            job_id="job-2",
            fingerprint=fingerprint_full,
            confirm=_confirm_replacement,
        )

    report = reconcile_source(source, confirm=_confirm_replacement)
    assert report.outcome is RecoveryOutcome.TEMP_REMOVED

    result = replace_with_verified_copy(
        local_output,
        source,
        expected_source=fingerprint_full(source),
        job_id="job-2",
        fingerprint=fingerprint_full,
        confirm=_confirm_replacement,
    )

    assert result.job_id == "job-2"
    assert source.read_bytes() == REPLACEMENT
    assert find_artifacts(source).all == ()
