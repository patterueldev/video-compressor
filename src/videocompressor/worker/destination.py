"""Destination-mode work for one claimed job (issues #40, #87).

The pipeline delegates the convert-flow destination here: the job's destination
spec and optional naming spec are decoded and resolved through this node's own
roots (:mod:`videocompressor.worker.destination_paths`), checked for an already
valid output (skip) or an occupied one (review), and finally delivered by
:func:`videocompressor.core.place.place_output` with the optional verified source
deletion. Same-folder jobs and legacy claims without a destination keep the
existing verified swap in :mod:`videocompressor.worker.transfer`; pilot mode wins
over a job destination (a pilot node is excluded from the convert flow).

Lifecycle: :func:`plan_destination` decodes and resolves the destination folder;
:meth:`DestinationJob.prepare` binds the final output name — the source name, or
the rendered name pattern using the claim's capture time first and the source's
raw tag as the fallback — and runs the early skip/collision check.
:meth:`DestinationJob.deliver` places the output and performs the guarded source
deletion last. Every step returns a
:class:`videocompressor.worker.outcome.JobOutcome` (or ``None`` when the pipeline
should keep going); an operator stop inside the placement copy propagates as
:class:`videocompressor.worker.scratch.CopyStopped` for the caller to requeue.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from videocompressor.core.models import EncodeSettings, Fingerprint
from videocompressor.core.naming import NamePatternError, needs_capture_time
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
from videocompressor.worker.destination_paths import (
    Destination,
    DestinationKind,
    DestinationSpecError,
    NamingSpec,
    destination_folder,
    destination_from_job,
    naming_from_job,
    naming_time_from_claim,
    naming_time_from_facts,
    output_name,
)
from videocompressor.worker.outcome import JobOutcome
from videocompressor.worker.scratch import CopyStopped, copy_with_stop

__all__ = [
    "Destination",
    "DestinationJob",
    "DestinationKind",
    "DestinationSpecError",
    "destination_from_job",
    "plan_destination",
]

_log = logging.getLogger("videocompressor.worker")

FingerprintFn = Callable[[Path], Fingerprint]
ValidateFn = Callable[..., ValidationResult]
ConfirmFn = Callable[[Path], bool]


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
    otherwise the returned job resolves its final output name in
    :meth:`DestinationJob.prepare` and is ready for the later pipeline steps.
    """
    try:
        destination = destination_from_job(job)
        naming = naming_from_job(job)
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
        folder = destination_folder(destination, config, job)
    except (DestinationSpecError, MappingError) as exc:
        return JobOutcome("failed", f"destination: {exc}")
    if naming is None:
        output_path = folder / source.name
        if output_path == source:
            if job.delete_original is False:
                return JobOutcome(
                    "failed",
                    "destination: resolving to the source path replaces the source, "
                    "so delete_original=false is not a valid combination",
                )
            return None
        for path in cleanup_destination(output_path):
            _log.info("removed leftover destination artifact %s", path)
        return DestinationJob(
            output_path,
            folder=folder,
            job=job,
            source=source,
            delete_original=job.delete_original is True,
            fingerprint=fingerprint,
            validate=validate,
            readable=readable,
        )
    return DestinationJob(
        None,
        folder=folder,
        job=job,
        source=source,
        delete_original=job.delete_original is True,
        naming=naming,
        fingerprint=fingerprint,
        validate=validate,
        readable=readable,
    )


class DestinationJob:
    """Deliver one job's validated output to its destination folder."""

    def __init__(
        self,
        output_path: Path | None,
        *,
        folder: Path,
        job: ClaimedJob,
        source: Path,
        delete_original: bool,
        fingerprint: FingerprintFn,
        validate: ValidateFn,
        readable: ConfirmFn,
        naming: NamingSpec | None = None,
    ) -> None:
        #: Final output path; ``None`` until a naming spec is rendered in ``prepare``.
        self.output_path = output_path
        self.folder = folder
        self.job = job
        self.source = source
        self.delete_original = delete_original
        self.naming = naming
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
        """Bind the output name and skip or park an existing output.

        A naming spec is rendered here — the claim's capture time first, the
        source's raw ``creation_time`` tag as the fallback — so skip, resume,
        and collision checks all use the final path. Returns ``None`` when
        there is nothing at the destination yet (the caller keeps going), a
        ``complete``/``skipped`` outcome for a valid output, or ``review`` for
        an occupied destination that is not this job's verified output.
        """
        self._settings = settings
        self._source_fingerprint = source_fingerprint
        self._source_facts = source_facts
        if self.output_path is None:
            blocked = self._bind_rendered_path(source_facts)
            if blocked is not None:
                return blocked
        assert self.output_path is not None  # bound above or by plan_destination
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
        output must still confirm it and stay readable. A pending naming spec is
        rendered from the claim's capture time only — the source is gone, so
        there are no facts left to fall back to. Returns ``None`` when that
        evidence is missing, so the caller keeps the existing ``source_missing``
        review behavior.
        """
        if self.output_path is None:
            if self.naming is None:
                return None
            when = naming_time_from_claim(self.naming, self.job)
            if when is None:
                return None
            try:
                self.output_path = self.folder / output_name(self.naming, self.source, when)
            except NamePatternError:
                return None
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
        assert self.output_path is not None  # bound by prepare()
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

    def _bind_rendered_path(self, source_facts: MediaFacts) -> JobOutcome | None:
        """Render the naming spec into ``output_path``; ``None`` when ready."""
        assert self.naming is not None  # a pending path always has a naming spec
        when = naming_time_from_claim(self.naming, self.job)
        if when is None:
            when = naming_time_from_facts(self.naming, source_facts)
        if when is None and needs_capture_time(self.naming.pattern):
            return JobOutcome(
                "failed",
                f"name pattern needs a capture time; none found for {self.source.name}",
            )
        try:
            rendered = self.folder / output_name(self.naming, self.source, when)
        except NamePatternError as exc:
            return JobOutcome("failed", f"name pattern: {exc}")
        if rendered == self.source:
            return JobOutcome(
                "failed",
                "destination: the name pattern resolves to the source file; choose a "
                "different pattern or destination folder",
            )
        self.output_path = rendered
        for path in cleanup_destination(rendered):
            _log.info("removed leftover destination artifact %s", path)
        return None

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
