"""Unit tests for the verified replacement protocol (core/replace.py)."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

from videocompressor.core.replace import (
    BACKUP_MARKER,
    TEMP_MARKER,
    ArtifactsPresentError,
    ReplacementConfirmationError,
    SourceChangedError,
    SourceMissingError,
    SwapError,
    VerificationError,
    backup_path_for,
    commit_replacement,
    find_artifacts,
    replace_with_verified_copy,
    sha256_file,
    stage_output,
    temp_path_for,
)

ORIGINAL = b"original source bytes" * 64
REPLACEMENT = b"encoded replacement bytes" * 80


def _make_pair(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "GS010001.MP4"
    local_output = tmp_path / "scratch-output.MP4"
    source.write_bytes(ORIGINAL)
    local_output.write_bytes(REPLACEMENT)
    return source, local_output


def _confirm_replacement(path: Path) -> bool:
    return path.read_bytes() == REPLACEMENT


def test_job_id_is_validated(tmp_path: Path):
    source = tmp_path / "clip.MP4"
    assert temp_path_for(source, "abc-1_2.3").name == "clip.MP4.vc-tmp.abc-1_2.3"
    for bad in ("", "a/b", "x" * 65, "sp ace", "tab\tid"):
        with pytest.raises(ValueError, match="job_id"):
            temp_path_for(source, bad)
        with pytest.raises(ValueError, match="job_id"):
            backup_path_for(source, bad)


def test_artifact_names_embed_job_id(tmp_path: Path):
    source = tmp_path / "clip.MP4"
    assert temp_path_for(source, "job-1").name == f"clip.MP4{TEMP_MARKER}job-1"
    assert backup_path_for(source, "job-1").name == f"clip.MP4{BACKUP_MARKER}job-1"
    assert temp_path_for(source, "job-2") != temp_path_for(source, "job-1")


def test_find_artifacts_matches_only_this_clip(tmp_path: Path):
    source = tmp_path / "clip.MP4"
    source.write_bytes(b"x")
    own_temp = temp_path_for(source, "job-1")
    own_backup = backup_path_for(source, "job-1")
    own_temp.write_bytes(b"t")
    own_backup.write_bytes(b"b")
    other = tmp_path / "other.MP4"
    temp_path_for(other, "job-1").write_bytes(b"t")
    (tmp_path / "clip.MP4.notes.txt").write_bytes(b"n")
    (tmp_path / "clip.MP4.vc-review.job-2").write_bytes(b"r")

    artifacts = find_artifacts(source)

    assert artifacts.temps == (own_temp,)
    assert artifacts.backups == (own_backup,)
    assert artifacts.all == (own_temp, own_backup)
    assert bool(artifacts)
    assert not find_artifacts(other.parent / "missing.MP4")


def test_sha256_file(tmp_path: Path):
    payload = b"verify me" * 1000
    path = tmp_path / "blob.bin"
    path.write_bytes(payload)

    size, digest = sha256_file(path, chunk_size=7)

    assert size == len(payload)
    assert digest == hashlib.sha256(payload).hexdigest()


def test_replace_happy_path(tmp_path: Path, fingerprint_full):
    source, local_output = _make_pair(tmp_path)
    expected = fingerprint_full(source)

    result = replace_with_verified_copy(
        local_output,
        source,
        expected_source=expected,
        job_id="job-1",
        fingerprint=fingerprint_full,
        confirm=_confirm_replacement,
    )

    assert source.read_bytes() == REPLACEMENT
    assert result.output == fingerprint_full(source)
    assert result.job_id == "job-1"
    assert result.backup_removed is True
    assert not find_artifacts(source)
    # the local scratch output is not touched by the protocol
    assert local_output.read_bytes() == REPLACEMENT


def test_source_change_aborts_before_any_write(tmp_path: Path, fingerprint_full):
    source, local_output = _make_pair(tmp_path)
    expected = fingerprint_full(source)
    source.write_bytes(ORIGINAL + b"tampered")

    with pytest.raises(SourceChangedError) as excinfo:
        replace_with_verified_copy(
            local_output,
            source,
            expected_source=expected,
            job_id="job-1",
            fingerprint=fingerprint_full,
            confirm=_confirm_replacement,
        )

    assert excinfo.value.source == source
    assert excinfo.value.expected == expected
    assert source.read_bytes() == ORIGINAL + b"tampered"
    assert not find_artifacts(source)
    assert local_output.read_bytes() == REPLACEMENT


def test_missing_source_is_reported(tmp_path: Path, fingerprint_full):
    source, local_output = _make_pair(tmp_path)
    expected = fingerprint_full(source)
    source.unlink()

    with pytest.raises(SourceMissingError, match="source is missing"):
        replace_with_verified_copy(
            local_output,
            source,
            expected_source=expected,
            job_id="job-1",
            fingerprint=fingerprint_full,
            confirm=_confirm_replacement,
        )

    assert not find_artifacts(source)
    assert local_output.read_bytes() == REPLACEMENT


def test_existing_artifacts_block_a_new_attempt(tmp_path: Path, fingerprint_full):
    source, local_output = _make_pair(tmp_path)
    stale_backup = backup_path_for(source, "crashed-job")
    stale_backup.write_bytes(b"stale backup")

    with pytest.raises(ArtifactsPresentError) as excinfo:
        replace_with_verified_copy(
            local_output,
            source,
            expected_source=fingerprint_full(source),
            job_id="job-2",
            fingerprint=fingerprint_full,
            confirm=_confirm_replacement,
        )

    assert stale_backup in excinfo.value.artifacts
    assert not temp_path_for(source, "job-2").exists()
    assert stale_backup.read_bytes() == b"stale backup"
    assert source.read_bytes() == ORIGINAL


def test_stage_output_verifies_readback(tmp_path: Path):
    source, local_output = _make_pair(tmp_path)

    staged = stage_output(local_output, source, job_id="job-1")

    assert staged.temp_path == temp_path_for(source, "job-1")
    assert staged.temp_path.read_bytes() == REPLACEMENT
    assert staged.size_bytes == len(REPLACEMENT)
    assert staged.sha256 == hashlib.sha256(REPLACEMENT).hexdigest()
    assert find_artifacts(source).temps == (staged.temp_path,)


def test_stage_output_cleans_temp_on_readback_mismatch(tmp_path: Path):
    source, local_output = _make_pair(tmp_path)

    def corrupting_copy(src: Path, dst: Path) -> None:
        shutil.copyfile(src, dst)
        with dst.open("r+b") as handle:
            handle.truncate(dst.stat().st_size - 1)

    with pytest.raises(VerificationError, match="read-back mismatch"):
        stage_output(local_output, source, job_id="job-1", copy_fn=corrupting_copy)

    assert not temp_path_for(source, "job-1").exists()


def test_stage_output_cleans_temp_on_copy_failure(tmp_path: Path):
    source, local_output = _make_pair(tmp_path)

    def exploding_copy(src: Path, dst: Path) -> None:
        raise OSError("No space left on device")

    with pytest.raises(OSError, match="No space"):
        stage_output(local_output, source, job_id="job-1", copy_fn=exploding_copy)

    assert not temp_path_for(source, "job-1").exists()


def test_stage_output_rejects_missing_local_output(tmp_path: Path):
    source = tmp_path / "clip.MP4"
    source.write_bytes(ORIGINAL)

    with pytest.raises(VerificationError, match="does not exist"):
        stage_output(tmp_path / "gone.MP4", source, job_id="job-1")

    assert not find_artifacts(source)


def test_commit_restores_backup_when_confirmation_fails(tmp_path: Path, fingerprint_full):
    source, local_output = _make_pair(tmp_path)
    expected = fingerprint_full(source)
    staged = stage_output(local_output, source, job_id="job-1")

    with pytest.raises(ReplacementConfirmationError, match="original was restored"):
        commit_replacement(
            source,
            staged,
            expected_source=expected,
            fingerprint=fingerprint_full,
            confirm=lambda path: False,
        )

    assert source.read_bytes() == ORIGINAL
    assert not find_artifacts(source)


def test_commit_confirmation_probe_error_restores_backup(tmp_path: Path, fingerprint_full):
    source, local_output = _make_pair(tmp_path)
    expected = fingerprint_full(source)
    staged = stage_output(local_output, source, job_id="job-1")

    def exploding_probe(path: Path) -> bool:
        raise ValueError("probe crashed")

    with pytest.raises(ReplacementConfirmationError, match="probe error"):
        commit_replacement(
            source,
            staged,
            expected_source=expected,
            fingerprint=fingerprint_full,
            confirm=exploding_probe,
        )

    assert source.read_bytes() == ORIGINAL
    assert not find_artifacts(source)


def test_commit_restores_backup_when_temp_vanished(tmp_path: Path, fingerprint_full):
    source, local_output = _make_pair(tmp_path)
    expected = fingerprint_full(source)
    staged = stage_output(local_output, source, job_id="job-1")
    staged.temp_path.unlink()

    with pytest.raises(SwapError, match="original was restored"):
        commit_replacement(
            source,
            staged,
            expected_source=expected,
            fingerprint=fingerprint_full,
            confirm=_confirm_replacement,
        )

    assert source.read_bytes() == ORIGINAL
    assert not find_artifacts(source)


def test_source_change_between_stage_and_commit_cleans_temp(
    tmp_path: Path, fingerprint_full
):
    source, local_output = _make_pair(tmp_path)
    expected = fingerprint_full(source)
    tampered = ORIGINAL + b"changed during transfer"

    def tamper_after_stage(step: str) -> None:
        if step == "temp_verified":
            source.write_bytes(tampered)

    with pytest.raises(SourceChangedError):
        replace_with_verified_copy(
            local_output,
            source,
            expected_source=expected,
            job_id="job-1",
            fingerprint=fingerprint_full,
            confirm=_confirm_replacement,
            on_step=tamper_after_stage,
        )

    assert source.read_bytes() == tampered
    assert not find_artifacts(source)


def test_records_folder_is_not_a_swap_artifact(tmp_path: Path):
    source = tmp_path / "GS010001.MP4"
    source.write_bytes(ORIGINAL)
    records = tmp_path / ".vc-records"
    records.mkdir()
    (records / f"{source.name}.vc-meta.json").write_text("{}", encoding="utf-8")

    artifacts = find_artifacts(source)

    assert not artifacts
    assert artifacts.temps == () and artifacts.backups == ()
