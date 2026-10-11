"""Destination placement protocol and sidecar records (issue #40).

Fast unit coverage without ffmpeg: the staging/verify/validate/record/rename
order, handled-failure rollback, the skip/collision classification, the guarded
source delete, and the crash matrix (a crash at every step must leave a state
that cleanup + a second attempt, or a skip, resolves without touching the source).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from videocompressor.core.place import (
    PLACE_CONFIRMED,
    PLACE_RECORD_WRITTEN,
    PLACE_RENAMED,
    PLACE_TEMP_VERIFIED,
    PLACE_TEMP_WRITTEN,
    PLACE_VALIDATED,
    DestinationArtifactsError,
    DestinationExistsError,
    PlaceError,
    PlacementValidationError,
    PlacementVerificationError,
    RecordWriteError,
    cleanup_destination,
    delete_source_verified,
    find_destination_artifacts,
    place_output,
    temp_path_for,
)
from videocompressor.core.records import (
    DestinationRecord,
    ExistingOutput,
    classify_existing_output,
    meta_path_for,
    read_record,
    write_record,
)
from videocompressor.core.replace import SourceChangedError, SourceMissingError

CLIP = "GS010001.MP4"
RELATIVE = "2024-06-Ride/GS010001.MP4"
ORIGINAL = b"original GoPro bytes" * 32
OUTPUT = b"compressed output bytes" * 24


class InjectedCrash(RuntimeError):
    """Stands in for a process death at a protocol step."""


def _prepare(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Return (source, local scratch output, destination path)."""
    source = tmp_path / "archive" / CLIP
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(ORIGINAL)
    local_output = tmp_path / "scratch.MP4"
    local_output.write_bytes(OUTPUT)
    destination = tmp_path / "out" / CLIP
    destination.parent.mkdir(parents=True, exist_ok=True)
    return source, local_output, destination


def _place(
    local_output: Path,
    destination: Path,
    *,
    source: Path,
    fingerprint,
    job_id: str = "job-1",
    **kwargs,
):
    return place_output(
        local_output,
        destination,
        job_id=job_id,
        root_id="archive",
        source_relative_path=RELATIVE,
        source_fingerprint=fingerprint(source),
        fingerprint=fingerprint,
        **kwargs,
    )


def _seed_output(destination: Path, source: Path, fingerprint, **overrides) -> DestinationRecord:
    """Write a placed output plus its matching record; returns the record."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(OUTPUT)
    record = DestinationRecord(
        root_id=overrides.pop("root_id", "archive"),
        source_relative_path=overrides.pop("relative_path", RELATIVE),
        clip=overrides.pop("clip", destination.name),
        source=fingerprint(source),
        output=fingerprint(destination),
        completed_at=datetime.now(UTC),
        **overrides,
    )
    write_record(destination, record)
    return record


def test_place_output_verifies_validates_records_then_renames(tmp_path, fingerprint_full):
    source, local_output, destination = _prepare(tmp_path)
    validated: list[Path] = []

    def validate(path: Path) -> bool:
        validated.append(path)
        assert not destination.exists()  # validated before the rename
        return True

    result = _place(
        local_output,
        destination,
        source=source,
        fingerprint=fingerprint_full,
        validate=validate,
    )

    assert destination.read_bytes() == OUTPUT
    assert validated == [temp_path_for(destination, "job-1")]
    assert result.record.version == 1
    assert result.record.clip == CLIP
    assert result.record.source == fingerprint_full(source)
    assert result.record.output == fingerprint_full(destination)
    assert read_record(destination) == result.record
    assert find_destination_artifacts(destination) == ()
    assert source.read_bytes() == ORIGINAL  # placement never touches the source


def test_place_output_read_back_mismatch_rolls_back(tmp_path, fingerprint_full):
    source, local_output, destination = _prepare(tmp_path)

    def corrupt_copy(origin: Path, target: Path) -> None:
        target.write_bytes(b"corrupted bytes")

    with pytest.raises(PlacementVerificationError, match="read-back mismatch"):
        _place(
            local_output,
            destination,
            source=source,
            fingerprint=fingerprint_full,
            copy_fn=corrupt_copy,
        )

    assert not destination.exists()
    assert find_destination_artifacts(destination) == ()
    assert not meta_path_for(destination).exists()


def test_place_output_validation_failure_rolls_back(tmp_path, fingerprint_full):
    source, local_output, destination = _prepare(tmp_path)

    with pytest.raises(PlacementValidationError, match="did not validate"):
        _place(
            local_output,
            destination,
            source=source,
            fingerprint=fingerprint_full,
            validate=lambda path: False,
        )

    assert not destination.exists()
    assert find_destination_artifacts(destination) == ()
    assert not meta_path_for(destination).exists()


def test_place_output_validation_error_rolls_back(tmp_path, fingerprint_full):
    source, local_output, destination = _prepare(tmp_path)

    def broken_validate(path: Path) -> bool:
        raise RuntimeError("probe exploded")

    with pytest.raises(PlacementValidationError, match="could not run"):
        _place(
            local_output,
            destination,
            source=source,
            fingerprint=fingerprint_full,
            validate=broken_validate,
        )

    assert not destination.exists()
    assert find_destination_artifacts(destination) == ()


def test_place_output_refuses_a_taken_destination(tmp_path, fingerprint_full):
    source, local_output, destination = _prepare(tmp_path)
    destination.write_bytes(b"user file")

    with pytest.raises(DestinationExistsError):
        _place(local_output, destination, source=source, fingerprint=fingerprint_full)

    assert destination.read_bytes() == b"user file"
    assert not meta_path_for(destination).exists()


def test_place_output_refuses_leftover_artifacts(tmp_path, fingerprint_full):
    source, local_output, destination = _prepare(tmp_path)
    leftover = temp_path_for(destination, "old-attempt")
    leftover.write_bytes(b"staged bytes")

    with pytest.raises(DestinationArtifactsError, match="cleanup first"):
        _place(local_output, destination, source=source, fingerprint=fingerprint_full)

    assert leftover.read_bytes() == b"staged bytes"
    assert not destination.exists()


def test_place_output_rejects_an_empty_local_output(tmp_path, fingerprint_full):
    source, local_output, destination = _prepare(tmp_path)
    local_output.write_bytes(b"")

    with pytest.raises(PlacementVerificationError, match="empty"):
        _place(local_output, destination, source=source, fingerprint=fingerprint_full)

    assert not destination.exists()


def test_place_output_rejects_a_missing_destination_folder(tmp_path, fingerprint_full):
    source, local_output, _ = _prepare(tmp_path)
    destination = tmp_path / "nowhere" / CLIP

    with pytest.raises(PlaceError, match="destination folder does not exist"):
        _place(local_output, destination, source=source, fingerprint=fingerprint_full)


def test_record_round_trip_and_degraded_reads(tmp_path, fingerprint_full):
    source, _, destination = _prepare(tmp_path)
    record = _seed_output(destination, source, fingerprint_full)
    assert read_record(destination) == record

    meta_path_for(destination).write_text("{not json", encoding="utf-8")
    assert read_record(destination) is None

    meta_path_for(destination).write_text('{"version": 99}', encoding="utf-8")
    assert read_record(destination) is None
    assert read_record(tmp_path / "no-record.MP4") is None


def test_cleanup_destination_is_scoped_and_idempotent(tmp_path, fingerprint_full):
    source, _, destination = _prepare(tmp_path)
    _seed_output(destination, source, fingerprint_full)
    destination.unlink()  # the record is now stale (its output is gone)
    leftover = temp_path_for(destination, "old-attempt")
    leftover.write_bytes(b"staged bytes")
    partial = destination.parent / f".{CLIP}.vc-meta.json.abc.tmp"
    partial.write_bytes(b"partial")
    foreign = destination.parent / "other-file.mp4"
    foreign.write_bytes(b"other")

    removed = cleanup_destination(destination)

    assert set(removed) == {leftover, partial, meta_path_for(destination)}
    assert not meta_path_for(destination).exists()
    assert foreign.read_bytes() == b"other"

    # an existing output plus its record survive cleanup, and a second run is a no-op
    record = _seed_output(destination, source, fingerprint_full)
    assert cleanup_destination(destination) == ()
    assert destination.read_bytes() == OUTPUT
    assert read_record(destination) == record


def test_classify_existing_output_skip_and_collisions(tmp_path, fingerprint_full):
    source, _, destination = _prepare(tmp_path)
    kwargs = {
        "root_id": "archive",
        "relative_path": RELATIVE,
        "fingerprint": fingerprint_full,
    }

    assert classify_existing_output(destination, **kwargs, source_fingerprint=None) == (
        ExistingOutput.NONE,
        None,
    )

    record = _seed_output(destination, source, fingerprint_full)
    assert classify_existing_output(
        destination, **kwargs, source_fingerprint=fingerprint_full(source)
    ) == (ExistingOutput.SKIP, record)

    # a failing validator never authorises a skip
    state, _ = classify_existing_output(
        destination,
        **kwargs,
        source_fingerprint=fingerprint_full(source),
        validate=lambda path: False,
    )
    assert state is ExistingOutput.COLLISION

    # a changed source, a modified output, another clip identity, and a missing
    # record are all collisions
    source.write_bytes(b"changed elsewhere")
    assert classify_existing_output(
        destination, **kwargs, source_fingerprint=fingerprint_full(source)
    )[0] is ExistingOutput.COLLISION

    source.write_bytes(ORIGINAL)
    destination.write_bytes(OUTPUT + b"tampered")
    assert classify_existing_output(
        destination, **kwargs, source_fingerprint=fingerprint_full(source)
    )[0] is ExistingOutput.COLLISION
    destination.write_bytes(OUTPUT)

    assert classify_existing_output(
        destination,
        **{**kwargs, "root_id": "other"},
        source_fingerprint=fingerprint_full(source),
    )[0] is ExistingOutput.COLLISION
    meta_path_for(destination).unlink()
    assert classify_existing_output(
        destination, **kwargs, source_fingerprint=fingerprint_full(source)
    ) == (ExistingOutput.COLLISION, None)


def test_delete_source_verified_only_deletes_a_matching_source(tmp_path, fingerprint_full):
    source, _, _ = _prepare(tmp_path)
    expected = fingerprint_full(source)

    source.write_bytes(b"changed since claim")
    with pytest.raises(SourceChangedError):
        delete_source_verified(source, expected=expected, fingerprint=fingerprint_full)
    assert source.read_bytes() == b"changed since claim"

    source.write_bytes(ORIGINAL)
    expected = fingerprint_full(source)  # the rewrite changed the mtime
    delete_source_verified(source, expected=expected, fingerprint=fingerprint_full)
    assert not source.exists()

    with pytest.raises(SourceMissingError):
        delete_source_verified(source, expected=expected, fingerprint=fingerprint_full)


CRASH_STEPS = [
    PLACE_TEMP_WRITTEN,
    PLACE_TEMP_VERIFIED,
    PLACE_VALIDATED,
    PLACE_RECORD_WRITTEN,
    PLACE_RENAMED,
    PLACE_CONFIRMED,
]


@pytest.mark.parametrize("crash_step", CRASH_STEPS)
def test_crash_at_each_step_then_resume(tmp_path, fingerprint_full, crash_step):
    source, local_output, destination = _prepare(tmp_path)

    def crash_hook(step: str) -> None:
        if step == crash_step:
            raise InjectedCrash(step)

    with pytest.raises(InjectedCrash):
        _place(
            local_output,
            destination,
            source=source,
            fingerprint=fingerprint_full,
            on_step=crash_hook,
        )

    assert source.read_bytes() == ORIGINAL  # the placement protocol never deletes
    kwargs = {
        "root_id": "archive",
        "relative_path": RELATIVE,
        "source_fingerprint": fingerprint_full(source),
        "fingerprint": fingerprint_full,
    }

    if crash_step in (PLACE_RENAMED, PLACE_CONFIRMED):
        # the output and its write-ahead record are complete: resume skips
        state, record = classify_existing_output(destination, **kwargs)
        assert state is ExistingOutput.SKIP and record is not None
        assert cleanup_destination(destination) == ()
        assert destination.read_bytes() == OUTPUT
        return

    # before the rename: cleanup reaps the temp and any stale/partial record, and
    # a second attempt converges on the same verified output
    assert cleanup_destination(destination) != ()
    assert not destination.exists()
    assert find_destination_artifacts(destination) == ()
    assert not meta_path_for(destination).exists()

    result = _place(local_output, destination, source=source, fingerprint=fingerprint_full)
    assert destination.read_bytes() == OUTPUT
    assert result.record.output == fingerprint_full(destination)
    assert cleanup_destination(destination) == ()


def test_crash_before_the_record_leaves_no_artifact_for_a_retry(tmp_path, fingerprint_full):
    source, local_output, destination = _prepare(tmp_path)

    def crash_hook(step: str) -> None:
        if step == PLACE_VALIDATED:
            raise InjectedCrash(step)

    with pytest.raises(InjectedCrash):
        _place(
            local_output,
            destination,
            source=source,
            fingerprint=fingerprint_full,
            on_step=crash_hook,
        )

    removed = cleanup_destination(destination)
    assert len(removed) == 1 and removed[0].name.startswith(CLIP)
    assert not meta_path_for(destination).exists()
    assert not destination.exists()


def test_record_write_failure_rolls_back_the_staged_temp(tmp_path, fingerprint_full, monkeypatch):
    source, local_output, destination = _prepare(tmp_path)

    def broken_write(output_path: Path, record: DestinationRecord) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr("videocompressor.core.place.write_record", broken_write)

    with pytest.raises(RecordWriteError, match="record"):
        _place(local_output, destination, source=source, fingerprint=fingerprint_full)

    assert not destination.exists()
    assert find_destination_artifacts(destination) == ()
    assert source.read_bytes() == ORIGINAL
