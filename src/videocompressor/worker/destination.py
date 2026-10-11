"""Destination-mode work for one claimed job (issue #40).

The pipeline delegates the convert-flow destination here: the job's destination
spec is decoded, resolved through this node's own roots, checked for an already
valid output (skip) or an occupied one (review), and finally delivered by
:func:`videocompressor.core.place.place_output` with the optional verified source
deletion. Same-folder jobs and legacy claims without a destination keep the
existing verified swap in :mod:`videocompressor.worker.transfer`; pilot mode wins
over a job destination (a pilot node is excluded from the convert flow).

Lifecycle: :func:`plan_destination` decodes, resolves, and cleans up leftovers;
:meth:`DestinationJob.prepare` binds the effective settings and runs the early
skip/collision check; :meth:`DestinationJob.deliver` places the output and
performs the guarded source deletion last. Every step returns a
:class:`videocompressor.worker.outcome.JobOutcome` (or ``None`` when the pipeline
should keep going); an operator stop inside the placement copy propagates as
:class:`videocompressor.worker.scratch.CopyStopped` for the caller to requeue.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from videocompressor.core.models import EncodeSettings, Fingerprint
from videocompressor.core.place import (
    DestinationArtifactsError,
    DestinationExistsError,
    PlaceError,
    cleanup_destination,
    delete_source_verified,
    place_output,
)
from videocompressor.core.records import ExistingOutput, classify_existing_output
from videocompressor.core.replace import (
    SourceChangedError,
    SourceMissingError,
    ffprobe_readable,
)
from videocompressor.core.validate import MediaFacts, ValidationResult
from videocompressor.worker.client import ClaimedJob
from videocompressor.worker.config import MappingError, WorkerConfig
from videocompressor.worker.outcome import JobOutcome
from videocompressor.worker.scratch import CopyStopped, copy_with_stop

__all__ = [
    "Destination",
    "DestinationJob",
    "DestinationKind",
    "DestinationSpecError",
    "destination_from_job",
    "destination_path",
    "plan_destination",
]

_log = logging.getLogger("videocompressor.worker")

FingerprintFn = Callable[[Path], Fingerprint]
ValidateFn = Callable[..., ValidationResult]
ConfirmFn = Callable[[Path], bool]


class DestinationKind(StrEnum):
    """The three destination forms of the convert flow (issue #39 wording)."""

    SAME_FOLDER = "same_folder"
    OUTPUT_ROOT = "output_root"
    CUSTOM_FOLDER = "custom_folder"


class DestinationSpecError(ValueError):
    """A claimed job's destination spec cannot be used on this node."""


@dataclass(frozen=True)
class Destination:
    """A decoded destination spec (``None`` destination means same-folder/legacy)."""

    kind: DestinationKind
    root_id: str | None = None
    relative_path: str | None = None
    folder: Path | None = None


def destination_from_job(job: ClaimedJob) -> Destination | None:
    """Decode the claim's destination field.

    A missing field (legacy claim) reads as ``None``. Malformed shapes raise
    :class:`DestinationSpecError`, which the caller reports as a ``failed`` job.
    """
    raw = job.destination
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise DestinationSpecError("'destination' must be an object")
    kind = raw.get("kind")
    if kind == DestinationKind.SAME_FOLDER:
        return Destination(kind=DestinationKind.SAME_FOLDER)
    if kind == DestinationKind.OUTPUT_ROOT:
        root_id = raw.get("root_id")
        if not isinstance(root_id, str) or not root_id:
            raise DestinationSpecError("output_root destinations need a root_id")
        relative = raw.get("relative_path")
        if relative is None:
            relative = job.relative_path
        if not isinstance(relative, str) or not relative:
            raise DestinationSpecError("output_root destinations need a relative_path")
        return Destination(
            kind=DestinationKind.OUTPUT_ROOT, root_id=root_id, relative_path=relative
        )
    if kind == DestinationKind.CUSTOM_FOLDER:
        folder = raw.get("path")
        if not isinstance(folder, str) or not folder:
            raise DestinationSpecError("custom_folder destinations need a path")
        path = Path(folder).expanduser()
        if not path.is_absolute():
            raise DestinationSpecError(f"custom_folder path must be absolute: {folder!r}")
        return Destination(kind=DestinationKind.CUSTOM_FOLDER, folder=path)
    raise DestinationSpecError(f"unknown destination kind: {kind!r}")


def destination_path(destination: Destination, config: WorkerConfig, source: Path) -> Path:
    """Resolve a custom destination to this node's output path.

    Registered output roots go through the worker's own mapping (relative, inside
    the root; intermediate folders are created). A custom folder must already
    exist on this node — it is never created silently. The output keeps the
    source clip's file name. Raises :class:`DestinationSpecError` (or
    :class:`MappingError` for an unmapped root).
    """
    if destination.kind is DestinationKind.OUTPUT_ROOT:
        if destination.root_id is None or destination.relative_path is None:
            raise DestinationSpecError("output_root destination is incomplete")
        resolved = config.resolve(destination.root_id, destination.relative_path)
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise DestinationSpecError(
                f"cannot create destination folder {resolved.parent}: {exc}"
            ) from exc
        return resolved
    if destination.kind is DestinationKind.CUSTOM_FOLDER:
        folder = destination.folder
        if folder is None:
            raise DestinationSpecError("custom_folder destination is incomplete")
        if not folder.is_dir():
            raise DestinationSpecError(
                f"destination folder does not exist on this node: {folder}"
            )
        return folder / source.name
    raise DestinationSpecError(f"{destination.kind} has no custom destination path")


def plan_destination(
    job: ClaimedJob,
    config: WorkerConfig,
    source: Path,
    *,
    pilot: bool,
    fingerprint: FingerprintFn,
    validate: ValidateFn,
    readable: ConfirmFn = ffprobe_readable,
) -> DestinationJob | JobOutcome | None:
    """Decode, resolve, and prepare the job's destination.

    Returns ``None`` for the same-folder swap (legacy claim, ``same_folder``
    spec, a destination that resolves to the source path, or pilot mode). A
    :class:`JobOutcome` is a terminal failure for a malformed or unusable spec;
    otherwise the returned job is ready for the later pipeline steps.
    """
    try:
        destination = destination_from_job(job)
    except DestinationSpecError as exc:
        return JobOutcome("failed", f"destination: {exc}")
    if destination is None or destination.kind is DestinationKind.SAME_FOLDER:
        if destination is not None and job.delete_original is False:
            return JobOutcome(
                "failed",
                "destination: a same-folder output replaces the source, so "
                "delete_original=false is not a valid combination",
            )
        return None
    if pilot:
        return None
    try:
        output_path = destination_path(destination, config, source)
    except (DestinationSpecError, MappingError) as exc:
        return JobOutcome("failed", f"destination: {exc}")
    if output_path == source:
        if job.delete_original is False:
            return JobOutcome(
                "failed",
                "destination: resolving to the source path replaces the source, "
                "so delete_original=false is not a valid combination",
            )
        return None
    resolved = DestinationJob(
        output_path,
        job=job,
        source=source,
        delete_original=job.delete_original is True,
        fingerprint=fingerprint,
        validate=validate,
        readable=readable,
    )
    for path in cleanup_destination(output_path):
        _log.info("removed leftover destination artifact %s", path)
    return resolved


class DestinationJob:
    """Deliver one job's validated output to its custom destination."""

    def __init__(
        self,
        output_path: Path,
        *,
        job: ClaimedJob,
        source: Path,
        delete_original: bool,
        fingerprint: FingerprintFn,
        validate: ValidateFn,
        readable: ConfirmFn,
    ) -> None:
        self.output_path = output_path
        self.job = job
        self.source = source
        self.delete_original = delete_original
        self._fingerprint = fingerprint
        self._validate = validate
        self._readable = readable
        self._settings: EncodeSettings | None = None
        self._source_fingerprint: Fingerprint | None = None
        self._source_facts: MediaFacts | None = None

    # -- early steps -------------------------------------------------------

    def prepare(
        self,
        settings: EncodeSettings,
        source_fingerprint: Fingerprint,
        source_facts: MediaFacts,
    ) -> JobOutcome | None:
        """Bind the effective settings and skip or park an existing output.

        Returns ``None`` when there is nothing at the destination yet (the
        caller keeps going), a ``complete``/``skipped`` outcome for a valid
        output, or ``review`` for an occupied destination that is not this job's
        verified output.
        """
        self._settings = settings
        self._source_fingerprint = source_fingerprint
        self._source_facts = source_facts
        state, record = classify_existing_output(
            self.output_path,
            root_id=self.job.root_id,
            relative_path=self.job.relative_path,
            source_fingerprint=source_fingerprint,
            validate=self._confirm(),
            fingerprint=self._fingerprint,
        )
        if state is ExistingOutput.NONE:
            return None
        if state is ExistingOutput.COLLISION or record is None:
            return JobOutcome(
                "review",
                f"destination: {self.output_path} already exists and is not this "
                "job's verified output; refusing to overwrite it",
                destination=self.output_path,
            )
        if self.delete_original:
            blocked = self._delete_source()
            if blocked is not None:
                return blocked
            detail = f"skipped: valid existing output at {self.output_path}; source deleted"
        else:
            detail = f"skipped: valid existing output at {self.output_path}"
        return JobOutcome(
            "complete",
            detail,
            output=record.output,
            skipped=True,
            destination=self.output_path,
        )

    def resume_without_source(self) -> JobOutcome | None:
        """Settle a job whose source is gone but whose output is already placed.

        The record written by the earlier attempt carries the clip identity; the
        output must still confirm it and stay readable. Returns ``None`` when
        that evidence is missing, so the caller keeps the existing
        ``source_missing`` review behavior.
        """
        state, record = classify_existing_output(
            self.output_path,
            root_id=self.job.root_id,
            relative_path=self.job.relative_path,
            source_fingerprint=None,
            validate=self._readable,
            fingerprint=self._fingerprint,
        )
        if state is not ExistingOutput.SKIP or record is None:
            return None
        return JobOutcome(
            "complete",
            f"skipped: source is gone and a verified output for this clip exists at "
            f"{self.output_path}",
            output=record.output,
            skipped=True,
            destination=self.output_path,
        )

    # -- delivery ----------------------------------------------------------

    def deliver(
        self, scratch_output: Path, *, job_id: str, stop_event: Any
    ) -> JobOutcome:
        """Place the validated output, then delete the source when requested."""
        assert self._source_fingerprint is not None  # bound by prepare()
        try:
            placement = place_output(
                scratch_output,
                self.output_path,
                job_id=job_id,
                root_id=self.job.root_id,
                source_relative_path=self.job.relative_path,
                source_fingerprint=self._source_fingerprint,
                template_id=self.job.template_id,
                settings_hash=self.job.settings_hash,
                copy_fn=lambda origin, destination: copy_with_stop(
                    origin, destination, stop_event
                ),
                validate=self._confirm(),
                fingerprint=self._fingerprint,
            )
        except CopyStopped:
            raise
        except (DestinationExistsError, DestinationArtifactsError) as exc:
            return JobOutcome("review", f"destination: {exc}")
        except PlaceError as exc:
            return JobOutcome("failed", f"destination placement failed: {exc}")
        except OSError as exc:
            return JobOutcome("failed", f"destination I/O error: {exc}")

        output = placement.record.output
        if not self.delete_original:
            return JobOutcome(
                "complete",
                f"output placed at {self.output_path}",
                output=output,
                destination=self.output_path,
            )
        blocked = self._delete_source()
        if blocked is not None:
            return blocked
        return JobOutcome(
            "complete",
            f"output placed at {self.output_path}; source deleted",
            output=output,
            destination=self.output_path,
        )

    # -- internals ---------------------------------------------------------

    def _confirm(self) -> ConfirmFn:
        assert self._settings is not None  # bound by prepare()

        def confirm(path: Path) -> bool:
            return bool(self._validate(path, self._settings, source=self._source_facts).ok)

        return confirm

    def _delete_source(self) -> JobOutcome | None:
        """Delete the source after a verified placement; never on a mismatch."""
        assert self._source_fingerprint is not None  # bound by prepare()
        try:
            delete_source_verified(
                self.source,
                expected=self._source_fingerprint,
                fingerprint=self._fingerprint,
            )
        except (SourceChangedError, SourceMissingError) as exc:
            return JobOutcome(
                "review", f"source was not deleted: {exc}", destination=self.output_path
            )
        except OSError as exc:
            return JobOutcome(
                "failed", f"could not delete the source: {exc}", destination=self.output_path
            )
        return None
