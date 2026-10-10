"""Verified replacement protocol: stage the encoded output beside the source on the
archive filesystem, verify the transfer by read-back, swap it into place behind a
recoverable backup, and delete the backup only once the replacement is confirmed
readable.

Everything here operates on the worker's locally resolved source path: an SMB mount on
macOS, a local filesystem on the Linux homeserver. The staged temp and the backup live
in the source's directory, so every rename stays on one filesystem and is atomic.
Artifact names embed a per-attempt job id, so concurrent jobs cannot collide, and a
new attempt refuses to start while artifacts exist; run
:func:`videocompressor.core.recover.reconcile_source` first. Handled failures restore
the backup before raising; a process crash leaves the on-disk state for recovery.

The fingerprint and readability checks are injected callables, so this module stays
independent of the probe (#2) and fingerprint (#3) implementations: callers pass real
implementations, tests pass fakes, and readability defaults to a direct ``ffprobe``
call.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from videocompressor.core.models import Fingerprint

#: Name components of the two swap artifacts, placed after the source's file name.
TEMP_MARKER = ".vc-tmp."
BACKUP_MARKER = ".vc-backup."

#: Step names reported through ``on_step`` right after the effect has happened. Tests
#: raise from the hook to simulate a crash at each point of the protocol.
STEP_TEMP_WRITTEN = "temp_written"
STEP_TEMP_VERIFIED = "temp_verified"
STEP_SOURCE_VERIFIED = "source_verified"
STEP_BACKUP_RENAMED = "backup_renamed"
STEP_SWAPPED = "swapped"
STEP_CONFIRMED = "confirmed"
STEP_BACKUP_DELETED = "backup_deleted"

FingerprintFn = Callable[[Path], Fingerprint]
ConfirmFn = Callable[[Path], bool]
CopyFn = Callable[[Path, Path], None]
StepHook = Callable[[str], None]

_JOB_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_CHUNK_SIZE = 1 << 20


class ReplaceError(RuntimeError):
    """Base class for replacement protocol failures."""


class SourceChangedError(ReplaceError):
    """The source no longer matches the fingerprint captured at claim (send to review)."""

    def __init__(self, source: Path, expected: Fingerprint, actual: Fingerprint) -> None:
        super().__init__(
            f"source changed since claim: {source} (expected {expected}, found {actual})"
        )
        self.source = source
        self.expected = expected
        self.actual = actual


class SourceMissingError(ReplaceError):
    """The source file is gone; the job must go back to review."""

    def __init__(self, source: Path) -> None:
        super().__init__(f"source is missing: {source}")
        self.source = source


class ArtifactsPresentError(ReplaceError):
    """Swap artifacts from an earlier attempt exist; recovery must run first."""

    def __init__(self, source: Path, artifacts: tuple[Path, ...]) -> None:
        names = ", ".join(sorted(path.name for path in artifacts))
        super().__init__(
            f"swap artifacts already exist for {source}: {names}; run recovery first"
        )
        self.source = source
        self.artifacts = artifacts


class VerificationError(ReplaceError):
    """The staged temp did not read back identical to the local output."""


class ReplacementConfirmationError(ReplaceError):
    """The replacement could not be confirmed readable; the original was restored."""


class SwapError(ReplaceError):
    """A filesystem rename in the swap failed; the original was restored when possible."""


class FfprobeUnavailableError(ReplaceError):
    """The ffprobe executable needed for the readability check is not available."""


@dataclass(frozen=True)
class SwapArtifacts:
    """Temp and backup files left beside a source by the replacement protocol."""

    temps: tuple[Path, ...]
    backups: tuple[Path, ...]

    @property
    def all(self) -> tuple[Path, ...]:
        """Every artifact, temps first."""
        return self.temps + self.backups

    def __bool__(self) -> bool:
        return bool(self.temps or self.backups)


@dataclass(frozen=True)
class StagedOutput:
    """A verified copy of the local output, waiting beside the source for the swap."""

    source: Path
    temp_path: Path
    job_id: str
    local_output: Path
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class ReplaceResult:
    """Outcome of a completed swap."""

    source: Path
    job_id: str
    output: Fingerprint
    #: False when the confirmed replacement could not have its backup removed; the
    #: swap itself is complete and recovery can clean the leftover later.
    backup_removed: bool = True


def temp_path_for(source: Path, job_id: str) -> Path:
    """Return the unique staged-output name for ``source`` and ``job_id``."""
    _validate_job_id(job_id)
    return source.with_name(source.name + TEMP_MARKER + job_id)


def backup_path_for(source: Path, job_id: str) -> Path:
    """Return the unique backup name for ``source`` and ``job_id``."""
    _validate_job_id(job_id)
    return source.with_name(source.name + BACKUP_MARKER + job_id)


def find_artifacts(source: Path) -> SwapArtifacts:
    """Find swap artifacts for ``source``, whatever job id produced them."""
    temps: list[Path] = []
    backups: list[Path] = []
    parent = source.parent
    if not parent.is_dir():
        return SwapArtifacts(temps=(), backups=())
    for entry in sorted(parent.iterdir()):
        if not entry.is_file():
            continue
        if entry.name.startswith(source.name + TEMP_MARKER):
            temps.append(entry)
        elif entry.name.startswith(source.name + BACKUP_MARKER):
            backups.append(entry)
    return SwapArtifacts(temps=tuple(temps), backups=tuple(backups))


def sha256_file(path: Path, *, chunk_size: int = _CHUNK_SIZE) -> tuple[int, str]:
    """Return ``(size_bytes, sha256_hexdigest)`` for ``path`` in one read pass."""
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def ffprobe_readable(
    path: Path, *, ffprobe_bin: str = "ffprobe", timeout_s: float = 60.0
) -> bool:
    """Return True when ``ffprobe`` can open ``path`` as real media with a video stream.

    This is the minimal "replacement is readable" confirmation; full validation
    (codec, height, duration, audio) lands with issue #4 and can be passed instead
    through the ``confirm`` parameter. The check rejects ffprobe's ``bin`` fallback
    format (macOS binary-data files, which would otherwise pass as ``bintext``
    video), so stray non-media files cannot be mistaken for a replacement.
    """
    command = [
        ffprobe_bin,
        "-v",
        "error",
        "-show_entries",
        "format=format_name:stream=codec_name,codec_type",
        "-of",
        "json",
        str(path),
    ]
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout_s
        )
    except FileNotFoundError as exc:
        raise FfprobeUnavailableError(
            f"ffprobe executable not found: {ffprobe_bin!r}"
        ) from exc
    except subprocess.TimeoutExpired:
        return False
    if completed.returncode != 0:
        return False
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, dict):
        return False
    format_block = payload.get("format")
    format_name = ""
    if isinstance(format_block, dict):
        format_name = str(format_block.get("format_name") or "")
    if "bin" in format_name.split(","):
        return False
    streams = payload.get("streams")
    if not isinstance(streams, list):
        return False
    for stream in streams:
        if not isinstance(stream, dict):
            continue
        codec_name = stream.get("codec_name")
        if stream.get("codec_type") == "video" and codec_name not in (None, "", "bintext"):
            return True
    return False


def stage_output(
    local_output: Path,
    source: Path,
    *,
    job_id: str,
    copy_fn: CopyFn | None = None,
    on_step: StepHook | None = None,
) -> StagedOutput:
    """Copy ``local_output`` to a unique temp file beside ``source`` and verify it.

    The temp is written on the archive filesystem (same directory as the source, so
    the later rename is atomic). Verification reads the temp back fully and compares
    size + SHA-256 against the local output. A read-back mismatch or copy failure
    removes the temp; pre-existing swap artifacts abort with
    :class:`ArtifactsPresentError`.
    """
    _validate_job_id(job_id)
    artifacts = find_artifacts(source)
    if artifacts:
        raise ArtifactsPresentError(source, artifacts.all)
    if not local_output.is_file():
        raise VerificationError(f"local output does not exist: {local_output}")

    expected_size, expected_sha = sha256_file(local_output)
    temp_path = temp_path_for(source, job_id)
    copier = copy_fn if copy_fn is not None else _copy_file
    try:
        copier(local_output, temp_path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    if on_step is not None:
        on_step(STEP_TEMP_WRITTEN)

    _fsync_best_effort(temp_path)
    try:
        actual_size, actual_sha = sha256_file(temp_path)
    except OSError as exc:
        temp_path.unlink(missing_ok=True)
        raise VerificationError(f"staged temp is unreadable: {temp_path}: {exc}") from exc
    if actual_size != expected_size or actual_sha != expected_sha:
        temp_path.unlink(missing_ok=True)
        raise VerificationError(
            f"read-back mismatch for {temp_path}: "
            f"expected {expected_size} bytes / {expected_sha}, "
            f"found {actual_size} bytes / {actual_sha}"
        )
    if on_step is not None:
        on_step(STEP_TEMP_VERIFIED)
    return StagedOutput(
        source=source,
        temp_path=temp_path,
        job_id=job_id,
        local_output=local_output,
        size_bytes=actual_size,
        sha256=actual_sha,
    )


def commit_replacement(
    source: Path,
    staged: StagedOutput,
    *,
    expected_source: Fingerprint,
    fingerprint: FingerprintFn,
    confirm: ConfirmFn | None = None,
    on_step: StepHook | None = None,
) -> ReplaceResult:
    """Swap a verified staged output into place behind a recoverable backup.

    Order: pre-flight source fingerprint check, rename source to a unique backup,
    atomically rename the temp into the original path, confirm the replacement is
    readable, then delete the backup. Handled failures restore the backup before
    raising; a process crash leaves backup and temp for recovery to reconcile.
    """
    if not source.is_file():
        raise SourceMissingError(source)
    actual = fingerprint(source)
    if actual != expected_source:
        raise SourceChangedError(source, expected=expected_source, actual=actual)
    if on_step is not None:
        on_step(STEP_SOURCE_VERIFIED)

    artifacts = find_artifacts(source)
    extra = tuple(path for path in artifacts.temps if path != staged.temp_path)
    if artifacts.backups or extra:
        raise ArtifactsPresentError(source, artifacts.backups + extra)
    if staged.temp_path.parent != source.parent:
        raise SwapError(f"staged temp is not beside the source: {staged.temp_path}")

    confirmer = confirm if confirm is not None else ffprobe_readable
    backup_path = backup_path_for(source, staged.job_id)
    try:
        os.rename(source, backup_path)
    except OSError as exc:
        raise SwapError(f"could not move source aside: {source}: {exc}") from exc
    if on_step is not None:
        on_step(STEP_BACKUP_RENAMED)

    try:
        os.rename(staged.temp_path, source)
    except OSError as exc:
        restore_error = _try_restore_backup(source, backup_path)
        raise SwapError(
            f"could not move staged output into place: {exc}{_restore_note(restore_error)}"
        ) from exc
    if on_step is not None:
        on_step(STEP_SWAPPED)

    try:
        readable = bool(confirmer(source))
    except Exception as exc:
        restore_error = _try_restore_backup(source, backup_path)
        raise ReplacementConfirmationError(
            f"replacement could not be confirmed (probe error: {exc})"
            f"{_restore_note(restore_error)}"
        ) from exc
    if not readable:
        restore_error = _try_restore_backup(source, backup_path)
        raise ReplacementConfirmationError(
            f"replacement at {source} is not readable{_restore_note(restore_error)}"
        )
    if on_step is not None:
        on_step(STEP_CONFIRMED)

    output = fingerprint(source)
    backup_removed = _try_unlink(backup_path)
    if backup_removed and on_step is not None:
        on_step(STEP_BACKUP_DELETED)
    return ReplaceResult(
        source=source,
        job_id=staged.job_id,
        output=output,
        backup_removed=backup_removed,
    )


def replace_with_verified_copy(
    local_output: Path,
    source: Path,
    *,
    expected_source: Fingerprint,
    job_id: str,
    fingerprint: FingerprintFn,
    confirm: ConfirmFn | None = None,
    copy_fn: CopyFn | None = None,
    on_step: StepHook | None = None,
) -> ReplaceResult:
    """Run the whole protocol: pre-flight, stage with read-back, verify, swap, clean up.

    The pre-flight check runs once before the transfer (so a changed source fails
    cheaply) and again in :func:`commit_replacement` right before the swap (so a
    change during a slow transfer cannot be overwritten). Handled protocol failures
    remove the staged temp; unexpected failures leave artifacts for recovery.
    """
    if not source.is_file():
        raise SourceMissingError(source)
    actual = fingerprint(source)
    if actual != expected_source:
        raise SourceChangedError(source, expected=expected_source, actual=actual)

    staged = stage_output(
        local_output, source, job_id=job_id, copy_fn=copy_fn, on_step=on_step
    )
    try:
        return commit_replacement(
            source,
            staged,
            expected_source=expected_source,
            fingerprint=fingerprint,
            confirm=confirm,
            on_step=on_step,
        )
    except ReplaceError:
        staged.temp_path.unlink(missing_ok=True)
        raise


def _validate_job_id(job_id: str) -> None:
    if not _JOB_ID_PATTERN.match(job_id):
        raise ValueError(
            "job_id must match [A-Za-z0-9._-]{1,64}: "
            f"{job_id!r}"
        )


def _copy_file(local_output: Path, temp_path: Path) -> None:
    shutil.copyfile(local_output, temp_path)


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


def _try_restore_backup(source: Path, backup_path: Path) -> str | None:
    try:
        os.replace(backup_path, source)
    except OSError as exc:
        return str(exc)
    return None


def _try_unlink(path: Path) -> bool:
    try:
        path.unlink()
    except OSError:
        return False
    return True


def _restore_note(restore_error: str | None) -> str:
    if restore_error is None:
        return "; the original was restored"
    return f"; restoring the original failed ({restore_error}), run recovery"
