"""Destination placement protocol (issue #40): copy a validated local output to an
explicit destination, verify the copy by read-back, validate it at the destination,
record it in the destination's hidden ``.vc-records/`` folder, and delete the source
only after all of that succeeded.

Same-folder jobs keep using :mod:`videocompressor.core.replace` (the verified swap);
this module covers the custom-destination part of the convert flow. Everything
operates on the worker's locally resolved paths: the staging temp lives in the
destination folder (same directory as the final file, so the later rename is
atomic), and it is named after the output plus a per-attempt job id.

Order (the record is written *before* the rename, so a process crash can never
leave a final output without its record):

1. stage ``<output name>.vc-tmp.<job_id>`` and verify it by full read-back SHA-256;
2. validate the staged file at the destination (injected ``validate`` callable);
3. write ``.vc-records/<output name>.vc-meta.json`` atomically
   (:mod:`videocompressor.core.records`);
4. rename the temp into place;
5. confirm size + sampled digest of the placed file;
6. ``delete_source_verified`` removes the source (only when the caller asks).

Handled failures remove the temp and, before the rename, the record too: a handled
failure never leaves an unprovenanced output behind. Only a process crash can,
which :func:`classify_existing_output` reports as a collision (review) on resume.
"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from videocompressor.core.fingerprint import fingerprint_confirms, fingerprint_file
from videocompressor.core.models import Fingerprint
from videocompressor.core.records import (
    META_SUFFIX,
    RECORDS_DIR,
    DestinationRecord,
    legacy_meta_path_for,
    meta_path_for,
    write_record,
)
from videocompressor.core.replace import (
    TEMP_MARKER,
    SourceChangedError,
    SourceMissingError,
    sha256_file,
)

__all__ = [
    "DestinationArtifactsError",
    "DestinationExistsError",
    "PLACE_CONFIRMED",
    "PLACE_RECORD_WRITTEN",
    "PLACE_RENAMED",
    "PLACE_TEMP_VERIFIED",
    "PLACE_TEMP_WRITTEN",
    "PLACE_VALIDATED",
    "PlaceError",
    "Placement",
    "PlacementValidationError",
    "PlacementVerificationError",
    "RecordWriteError",
    "cleanup_destination",
    "delete_source_verified",
    "find_destination_artifacts",
    "place_output",
    "temp_path_for",
]

#: Step names reported through ``on_step`` right after the effect has happened. Tests
#: raise from the hook to simulate a crash at each point of the protocol.
PLACE_TEMP_WRITTEN = "temp_written"
PLACE_TEMP_VERIFIED = "temp_verified"
PLACE_VALIDATED = "validated"
PLACE_RECORD_WRITTEN = "record_written"
PLACE_RENAMED = "renamed"
PLACE_CONFIRMED = "confirmed"

FingerprintFn = Callable[[Path], Fingerprint]
ConfirmFn = Callable[[Path], bool]
CopyFn = Callable[[Path, Path], None]
StepHook = Callable[[str], None]

_JOB_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class PlaceError(RuntimeError):
    """Base class for destination placement failures."""


class DestinationExistsError(PlaceError):
    """The destination path is already taken; nothing was overwritten."""

    def __init__(self, output_path: Path) -> None:
        super().__init__(f"destination already exists: {output_path}")
        self.output_path = output_path


class DestinationArtifactsError(PlaceError):
    """Leftover destination artifacts from an earlier attempt exist."""

    def __init__(self, output_path: Path, artifacts: tuple[Path, ...]) -> None:
        names = ", ".join(sorted(path.name for path in artifacts))
        super().__init__(
            f"destination artifacts already exist for {output_path}: {names}; "
            "run cleanup first"
        )
        self.output_path = output_path
        self.artifacts = artifacts


class PlacementVerificationError(PlaceError):
    """The staged copy did not read back identical to the local output."""


class PlacementValidationError(PlaceError):
    """The staged output did not validate at the destination."""


class RecordWriteError(PlaceError):
    """The destination record could not be written; nothing was renamed."""


@dataclass(frozen=True)
class Placement:
    """Outcome of a completed placement."""

    output: Path
    record: DestinationRecord
    size_bytes: int
    sha256: str


def temp_path_for(output_path: Path, job_id: str) -> Path:
    """Return the staging temp name for ``output_path`` and ``job_id``."""
    _validate_job_id(job_id)
    return output_path.with_name(output_path.name + TEMP_MARKER + job_id)


def find_destination_artifacts(output_path: Path) -> tuple[Path, ...]:
    """Find staging temps and partial record files for ``output_path``.

    Staging temps live beside the output; partial record writes live in the
    hidden ``.vc-records/`` folder, with legacy partials beside the output
    found as well so older crashes are still cleaned up.
    """
    parent = output_path.parent
    if not parent.is_dir():
        return ()
    temp_prefix = output_path.name + TEMP_MARKER
    meta_prefix = f".{output_path.name}{META_SUFFIX}."
    artifacts: list[Path] = []
    for entry in sorted(parent.iterdir()):
        if not entry.is_file():
            continue
        if entry.name.startswith(temp_prefix):
            artifacts.append(entry)
        elif entry.name.startswith(meta_prefix) and entry.name.endswith(".tmp"):
            artifacts.append(entry)
    records_dir = parent / RECORDS_DIR
    if records_dir.is_dir():
        for entry in sorted(records_dir.iterdir()):
            if (
                entry.is_file()
                and entry.name.startswith(meta_prefix)
                and entry.name.endswith(".tmp")
            ):
                artifacts.append(entry)
    return tuple(artifacts)


def cleanup_destination(output_path: Path) -> tuple[Path, ...]:
    """Remove this clip's leftover artifacts; returns the removed paths.

    Idempotent and scoped to ``output_path``: staging temps and partial record
    writes are always removed, records (new or legacy) only when their output
    file is absent, and the output itself plus foreign files are never touched.
    """
    removed: list[Path] = []
    for artifact in find_destination_artifacts(output_path):
        if _try_unlink(artifact):
            removed.append(artifact)
    if not output_path.is_file():
        for stale in (meta_path_for(output_path), legacy_meta_path_for(output_path)):
            if _try_unlink(stale):
                removed.append(stale)
    return tuple(removed)


def place_output(
    local_output: Path,
    output_path: Path,
    *,
    job_id: str,
    root_id: str,
    source_relative_path: str,
    source_fingerprint: Fingerprint,
    template_id: str | None = None,
    settings_hash: str | None = None,
    copy_fn: CopyFn | None = None,
    validate: ConfirmFn | None = None,
    fingerprint: FingerprintFn = fingerprint_file,
    on_step: StepHook | None = None,
) -> Placement:
    """Place ``local_output`` at ``output_path`` behind a verified, recorded copy.

    Refuses a taken destination or leftover artifacts, stages and verifies the
    temp, validates it in the destination folder, writes the record, renames the
    temp into place, and confirms the placed file. Handled failures roll back to
    the pre-attempt state; the source is never touched here.
    """
    _validate_job_id(job_id)
    if output_path.exists():
        raise DestinationExistsError(output_path)
    if not output_path.parent.is_dir():
        raise PlaceError(f"destination folder does not exist: {output_path.parent}")
    artifacts = find_destination_artifacts(output_path)
    if artifacts:
        raise DestinationArtifactsError(output_path, artifacts)
    if not local_output.is_file():
        raise PlacementVerificationError(f"local output does not exist: {local_output}")

    expected_size, expected_sha = sha256_file(local_output)
    if expected_size <= 0:
        raise PlacementVerificationError(f"local output is empty: {local_output}")

    temp_path = temp_path_for(output_path, job_id)
    copier = copy_fn if copy_fn is not None else shutil.copyfile
    try:
        copier(local_output, temp_path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    if on_step is not None:
        on_step(PLACE_TEMP_WRITTEN)

    _fsync_best_effort(temp_path)
    try:
        actual_size, actual_sha = sha256_file(temp_path)
    except OSError as exc:
        temp_path.unlink(missing_ok=True)
        raise PlacementVerificationError(
            f"staged output is unreadable: {temp_path}: {exc}"
        ) from exc
    if actual_size != expected_size or actual_sha != expected_sha:
        temp_path.unlink(missing_ok=True)
        raise PlacementVerificationError(
            f"read-back mismatch for {temp_path}: expected {expected_size} bytes / "
            f"{expected_sha}, found {actual_size} bytes / {actual_sha}"
        )
    if on_step is not None:
        on_step(PLACE_TEMP_VERIFIED)

    if validate is not None:
        try:
            validated = bool(validate(temp_path))
        except Exception as exc:
            temp_path.unlink(missing_ok=True)
            raise PlacementValidationError(
                f"destination validation could not run on {temp_path}: {exc}"
            ) from exc
        if not validated:
            temp_path.unlink(missing_ok=True)
            raise PlacementValidationError(
                f"staged output did not validate at the destination: {temp_path}"
            )
    if on_step is not None:
        on_step(PLACE_VALIDATED)

    record = DestinationRecord(
        root_id=root_id,
        source_relative_path=source_relative_path,
        clip=output_path.name,
        source=source_fingerprint,
        output=fingerprint(temp_path),
        template_id=template_id,
        settings_hash=settings_hash,
        completed_at=datetime.now(UTC),
    )
    try:
        write_record(output_path, record)
    except OSError as exc:
        temp_path.unlink(missing_ok=True)
        raise RecordWriteError(
            f"could not write the destination record for {output_path}: {exc}"
        ) from exc
    if on_step is not None:
        on_step(PLACE_RECORD_WRITTEN)

    try:
        os.rename(temp_path, output_path)
    except OSError as exc:
        _try_unlink(meta_path_for(output_path))
        temp_path.unlink(missing_ok=True)
        raise PlaceError(f"could not move the staged output into place: {exc}") from exc
    if on_step is not None:
        on_step(PLACE_RENAMED)

    if not _confirms_placed(output_path, record.output, fingerprint):
        output_path.unlink(missing_ok=True)
        _try_unlink(meta_path_for(output_path))
        raise PlaceError(f"placed output could not be confirmed at {output_path}")
    if on_step is not None:
        on_step(PLACE_CONFIRMED)

    return Placement(
        output=output_path, record=record, size_bytes=actual_size, sha256=actual_sha
    )


def delete_source_verified(
    source: Path, *, expected: Fingerprint, fingerprint: FingerprintFn = fingerprint_file
) -> None:
    """Delete ``source`` only while it still matches ``expected``.

    A missing source raises :class:`SourceMissingError`; a source that changed
    since the claim raises :class:`SourceChangedError` — a changed file is never
    deleted. The unlink itself is a single atomic operation.
    """
    if not source.is_file():
        raise SourceMissingError(source)
    actual = fingerprint(source)
    if actual != expected:
        raise SourceChangedError(source, expected=expected, actual=actual)
    source.unlink()


def _confirms_placed(
    output_path: Path, recorded: Fingerprint, fingerprint: FingerprintFn
) -> bool:
    try:
        return output_path.is_file() and fingerprint_confirms(
            recorded, fingerprint(output_path)
        )
    except OSError:
        return False


def _validate_job_id(job_id: str) -> None:
    if not _JOB_ID_PATTERN.match(job_id):
        raise ValueError(f"job_id must match [A-Za-z0-9._-]{{1,64}}: {job_id!r}")


def _try_unlink(path: Path) -> bool:
    try:
        path.unlink()
    except OSError:
        return False
    return True


def _fsync_best_effort(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
