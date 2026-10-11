"""Per-job worker pipeline: copy → encode → validate → replace (issue #6).

One job at a time per node (the agent serializes calls). The pipeline resolves
the job's archive-root mapping, passes the free-space gate, copies the source to
local scratch and verifies the copy, probes and encodes locally with the two-pass
recipe (issue #4), validates locally and after transfer (issue #4), and swaps the
output into place with the verified replacement protocol (issue #5). Pilot mode
delivers the output into a separate pilot root and never touches the source.

The archive is reached only through the worker's resolved local path; every
external effect is a seam (``PipelineSeams``) so tests run without ffmpeg.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from videocompressor.core.ffmpeg import (
    EncodeError,
    EncodeProgress,
    EncodeResult,
    EncodeStopped,
    ScratchSpaceError,
    ScratchSpaceReport,
    check_scratch_space,
    estimate_output_bytes,
    run_two_pass_encode,
)
from videocompressor.core.fingerprint import fingerprint_confirms, fingerprint_file
from videocompressor.core.markers import mark_completed
from videocompressor.core.models import EncodeSettings, Fingerprint, MediaInfo
from videocompressor.core.probe import FFprobeUnavailableError, ProbeError, probe_media
from videocompressor.core.recover import RecoveryOutcome, RecoveryReport, reconcile_source
from videocompressor.core.replace import (
    ArtifactsPresentError,
    ReplaceError,
    ReplacementConfirmationError,
    ReplaceResult,
    SourceChangedError,
    SourceMissingError,
    SwapError,
    VerificationError,
    ffprobe_readable,
    replace_with_verified_copy,
)
from videocompressor.core.validate import (
    ValidationResult,
    facts_from_media_info,
    validate_output,
)
from videocompressor.worker.client import ClaimedJob
from videocompressor.worker.config import MappingError, WorkerConfig
from videocompressor.worker.destination import DestinationJob, plan_destination
from videocompressor.worker.outcome import JobOutcome
from videocompressor.worker.scratch import (
    CopyStopped,
    copy_with_stop,
    discard,
    new_job_dir,
    new_job_id,
)
from videocompressor.worker.transfer import (
    PilotDeliveryError,
    deliver_to_pilot,
    replace_at_source,
)

__all__ = ["JobOutcome", "JobPipeline", "PipelineSeams"]

_log = logging.getLogger("videocompressor.worker")


@dataclass(frozen=True)
class PipelineSeams:
    """Injectable external effects; production defaults, test fakes otherwise."""

    encode: Callable[..., EncodeResult] = run_two_pass_encode
    probe: Callable[[Path], MediaInfo] = probe_media
    fingerprint: Callable[[Path], Fingerprint] = fingerprint_file
    replace: Callable[..., ReplaceResult] = replace_with_verified_copy
    validate: Callable[..., ValidationResult] = validate_output
    recover: Callable[..., RecoveryReport] = reconcile_source
    scratch_gate: Callable[..., ScratchSpaceReport] = check_scratch_space
    readable: Callable[[Path], bool] = ffprobe_readable


class JobPipeline:
    """Run one claimed job end to end on this node."""

    def __init__(
        self,
        config: WorkerConfig,
        *,
        seams: PipelineSeams | None = None,
        on_phase: Callable[[str], None] | None = None,
        on_progress: Callable[[EncodeProgress], None] | None = None,
    ) -> None:
        self._config = config
        self._seams = seams if seams is not None else PipelineSeams()
        self._on_phase = on_phase
        self._on_progress = on_progress

    def run(self, job: ClaimedJob, *, stop_event: Any) -> JobOutcome:
        """Process one job; always cleans its scratch tree before returning."""
        job_id = new_job_id(self._config.worker_id)
        job_dir = new_job_dir(self._config.scratch_dir, job_id)
        try:
            return self._run(job, job_id=job_id, job_dir=job_dir, stop_event=stop_event)
        finally:
            discard(job_dir)

    # -- steps -------------------------------------------------------------

    def _run(
        self, job: ClaimedJob, *, job_id: str, job_dir: Path, stop_event: Any
    ) -> JobOutcome:
        try:
            source = self._config.resolve(job.root_id, job.relative_path)
        except MappingError as exc:
            return JobOutcome("failed", f"mapping: {exc}")

        planned = plan_destination(
            job,
            self._config,
            source,
            pilot=self._config.pilot_root is not None,
            fingerprint=self._seams.fingerprint,
            validate=self._seams.validate,
            readable=self._seams.readable,
        )
        if isinstance(planned, JobOutcome):
            return planned
        destination = planned

        recovered = self._recover(job, source, destination_job=destination)
        if isinstance(recovered, JobOutcome):
            return recovered
        source = recovered

        settings = _settings_from_job(job)
        if settings is None:
            return JobOutcome("failed", "claim carried invalid encoding settings")

        try:
            source_fp = self._seams.fingerprint(source)
            source_bytes = source.stat().st_size
        except OSError as exc:
            return JobOutcome("review", f"cannot read source: {exc}")

        probed = self._probe_source(source)
        if isinstance(probed, JobOutcome):
            return probed
        source_media = probed
        if source_media.duration_s is None:
            return JobOutcome("review", "source reports no duration")

        if destination is not None:
            existing = destination.prepare(
                settings, source_fp, facts_from_media_info(source_media)
            )
            if existing is not None:
                return existing

        estimated = estimate_output_bytes(
            source_media.duration_s, settings.video_bitrate_kbps, settings.audio_bitrate_kbps
        )
        try:
            self._seams.scratch_gate(
                self._config.scratch_dir,
                source_bytes=source_bytes,
                estimated_output_bytes=estimated,
            )
        except ScratchSpaceError as exc:
            return JobOutcome("failed", f"scratch space: {exc}")

        scratch_source = job_dir / "source" / source.name
        try:
            copy_with_stop(source, scratch_source, stop_event)
        except CopyStopped as exc:
            return JobOutcome("requeue", f"stopped: {exc}")
        except OSError as exc:
            return JobOutcome("failed", f"copy failed: {exc}")
        try:
            copied = self._seams.fingerprint(scratch_source)
        except OSError as exc:
            return JobOutcome("failed", f"scratch copy unreadable: {exc}")
        if not fingerprint_confirms(source_fp, copied):
            return JobOutcome(
                "failed", "copy verification failed: scratch copy differs from source"
            )

        probed = self._probe_source(scratch_source, label="scratch copy")
        if isinstance(probed, JobOutcome):
            return probed
        local_media = probed
        if local_media.duration_s is None:
            return JobOutcome("review", "scratch copy reports no duration")
        source_facts = facts_from_media_info(local_media)

        self._emit_phase("encoding")
        scratch_output = job_dir / "out" / source.name
        try:
            self._seams.encode(
                scratch_source,
                scratch_output,
                settings,
                duration_s=local_media.duration_s,
                passlog_prefix=job_dir / "passlog",
                threads=self._config.thread_cap,
                stop_event=stop_event,
                on_progress=self._on_progress,
            )
        except EncodeStopped as exc:
            return JobOutcome("requeue", f"stopped: {exc}")
        except EncodeError as exc:
            return JobOutcome("failed", f"encode failed: {exc}")
        if not scratch_output.is_file():
            return JobOutcome("failed", "encode produced no output file")

        try:
            validation = self._seams.validate(scratch_output, settings, source=source_facts)
        except ValueError as exc:
            return JobOutcome("review", f"validation could not run: {exc}")
        if not validation.ok:
            return JobOutcome("failed", "validation failed: " + "; ".join(validation.failures))
        self._emit_phase("validated")

        if stop_event.is_set():
            return JobOutcome("requeue", "stopped before transfer")

        self._emit_phase("transferring")
        if self._config.pilot_root is not None:
            return self._deliver_pilot(job, scratch_output, stop_event)
        if destination is not None:
            try:
                return destination.deliver(
                    scratch_output, job_id=job_id, stop_event=stop_event
                )
            except CopyStopped as exc:
                return JobOutcome("requeue", f"stopped: {exc}")

        try:
            result = replace_at_source(
                scratch_output,
                source,
                settings=settings,
                source_facts=source_facts,
                expected_source=source_fp,
                job_id=job_id,
                fingerprint=self._seams.fingerprint,
                replace=self._seams.replace,
                validate=self._seams.validate,
                on_phase=self._emit_phase,
            )
        except VerificationError as exc:
            return JobOutcome("failed", f"transfer verification failed: {exc}")
        except (
            SourceChangedError,
            SourceMissingError,
            ArtifactsPresentError,
            ReplacementConfirmationError,
            SwapError,
        ) as exc:
            return JobOutcome("review", f"replacement needs review: {exc}")
        except ReplaceError as exc:
            return JobOutcome("review", f"replacement failed: {exc}")
        except OSError as exc:
            return JobOutcome("failed", f"replacement I/O error: {exc}")

        self._write_marker(job, source, result.output)
        return JobOutcome("complete", output=result.output)

    def _recover(
        self,
        job: ClaimedJob,
        source: Path,
        *,
        destination_job: DestinationJob | None = None,
    ) -> Path | JobOutcome:
        """Reconcile crash artifacts before working; returns the source or an outcome."""
        try:
            report = self._seams.recover(source)
        except Exception as exc:  # recovery must never abort a job unmanaged
            return JobOutcome("review", f"recovery failed: {exc}")
        if report.outcome is RecoveryOutcome.SOURCE_MISSING and destination_job is not None:
            resumed = destination_job.resume_without_source()
            if resumed is not None:
                return resumed
        if report.outcome in (RecoveryOutcome.SOURCE_MISSING, RecoveryOutcome.NEEDS_REVIEW):
            return JobOutcome("review", f"recovery: {report.detail or report.outcome}")
        if report.outcome is RecoveryOutcome.REPLACEMENT_CONFIRMED:
            output = self._seams.fingerprint(source)
            self._write_marker(job, source, output)
            return JobOutcome(
                "complete", "recovery confirmed an earlier replacement", output=output
            )
        if not source.is_file():
            return JobOutcome("review", f"source is missing: {source}")
        return source

    def _probe_source(self, path: Path, *, label: str = "source") -> MediaInfo | JobOutcome:
        try:
            return self._seams.probe(path)
        except ProbeError as exc:
            return JobOutcome("review", f"{label} unreadable: {exc}")
        except FFprobeUnavailableError as exc:
            return JobOutcome("failed", f"ffprobe unavailable: {exc}")

    def _deliver_pilot(
        self, job: ClaimedJob, scratch_output: Path, stop_event: Any
    ) -> JobOutcome:
        pilot_root = self._config.pilot_root
        assert pilot_root is not None  # guarded by the caller
        try:
            destination, _ = deliver_to_pilot(
                scratch_output,
                pilot_root,
                job.relative_path,
                stop_event,
                fingerprint=self._seams.fingerprint,
            )
        except CopyStopped as exc:
            return JobOutcome("requeue", f"stopped: {exc}")
        except (PilotDeliveryError, OSError) as exc:
            return JobOutcome("failed", f"pilot transfer failed: {exc}")
        return JobOutcome(
            "review",
            f"pilot output written to {destination}; originals untouched",
            pilot=True,
        )

    def _write_marker(self, job: ClaimedJob, source: Path, output: Fingerprint) -> None:
        try:
            mark_completed(
                source.parent,
                filename=source.name,
                output=output,
                template_id=job.template_id,
                settings_hash=job.settings_hash,
            )
        except OSError as exc:
            _log.warning("could not write the folder marker for %s: %s", source, exc)

    def _emit_phase(self, phase: str) -> None:
        if self._on_phase is not None:
            self._on_phase(phase)


def _settings_from_job(job: ClaimedJob) -> EncodeSettings | None:
    payload: Mapping[str, Any] = job.settings
    try:
        return EncodeSettings(
            output_height=_as_int(payload, "output_height", default=720),
            video_bitrate_kbps=_as_int(payload, "video_bitrate_kbps"),
            audio_bitrate_kbps=_as_int(payload, "audio_bitrate_kbps"),
            ffmpeg_preset=str(payload.get("ffmpeg_preset", "medium")),
        )
    except (TypeError, ValueError):
        return None


def _as_int(payload: Mapping[str, Any], key: str, *, default: int | None = None) -> int:
    value = payload.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key!r} must be an integer")
    return value
