"""Job-dispatch mixin for :class:`ControllerServer`.

Claim/complete/fail plus the manifest stage-transition helpers, moved verbatim
from ``controller/server.py`` (issue #21). The methods operate on the state
created by ``ControllerServer.__init__`` (``store``, ``manifest``, ``templates``,
``_lock``, ``_progress``, ``_clock``, ``_active_run_id``) and on shared lookups
kept on the facade (``_find_clip``, ``_event``). The mixin has no ``__init__``
of its own.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from videocompressor.controller.constants import LEASE_STATES
from videocompressor.controller.protocol import (
    ControllerError,
    _optional_int,
    _optional_str,
    _require_int,
    _require_str,
    _settings_hash,
    _unknown_worker,
)
from videocompressor.controller.store import WorkerRow
from videocompressor.core.config import DEFAULT_TEMPLATE_NAME
from videocompressor.core.models import (
    ClipRecord,
    Fingerprint,
    JobState,
    TemplateSettings,
)

#: Progress phases a worker reports that also advance the manifest stage.
_PHASE_STATES = {
    "copying": JobState.COPIED,
    "copied": JobState.COPIED,
    "encoding": JobState.ENCODING,
    "validating": JobState.VALIDATED,
    "validated": JobState.VALIDATED,
    "transferring": JobState.TRANSFERRING,
    "replacing": JobState.REPLACING,
}
_STAGE_ORDER = (
    JobState.CLAIMED,
    JobState.COPIED,
    JobState.ENCODING,
    JobState.VALIDATED,
    JobState.TRANSFERRING,
    JobState.REPLACING,
)


class JobDispatchMixin:
    """Claiming, completion, failure, and stage transitions."""

    def claim(self, worker_id: str) -> dict[str, Any] | None:
        """Claim the next queued clip; None when nothing is available."""
        with self._lock:
            if self.store.get_worker(worker_id) is None:
                raise _unknown_worker(worker_id)
            self.store.touch_worker(worker_id)
            self._mark_online(worker_id)
            record = self.manifest.claim_next(worker_id)
            if record is None:
                return None
            template_id, template = self._template_for(record)
            settings = self._effective_settings(template, record)
            settings_hash = _settings_hash(settings)
            if record.template_id != template_id or record.settings_hash != settings_hash:
                self.manifest.update_state(
                    record.root_id,
                    record.relative_path,
                    JobState.CLAIMED,
                    template_id=template_id,
                    settings_hash=settings_hash,
                )
            capture_time = record.capture_time
            self._progress.pop(worker_id, None)
            self._event(
                "info",
                f"claimed {record.relative_path}",
                worker_id=worker_id,
                root_id=record.root_id,
                relative_path=record.relative_path,
            )
            return {
                "job": {
                    "root_id": record.root_id,
                    "relative_path": record.relative_path,
                    "template_id": template_id,
                    "settings": settings,
                    "settings_hash": settings_hash,
                    "capture_time": (
                        capture_time.when.isoformat()
                        if capture_time is not None and capture_time.when is not None
                        else None
                    ),
                }
            }

    def complete(self, payload: Mapping[str, Any]) -> None:
        """Mark the held clip complete with its output fingerprint."""
        with self._lock:
            worker_id, root_id, relative_path, _ = self._held_clip(payload)
            output = payload.get("output")
            if not isinstance(output, Mapping):
                raise ControllerError(400, "bad_request", "'output' must be an object")
            size_bytes = _require_int(output, "size_bytes")
            digest = _require_str(output, "digest")
            mtime_ns = _optional_int(output, "mtime_ns", default=0)
            self.manifest.update_state(
                root_id,
                relative_path,
                JobState.COMPLETE,
                output=Fingerprint(size_bytes=size_bytes, mtime_ns=mtime_ns, digest=digest),
                completed_at=self._clock(),
                error=None,
            )
            self._finish_job(worker_id)
            self._event(
                "info",
                f"completed {relative_path}",
                worker_id=worker_id,
                root_id=root_id,
                relative_path=relative_path,
            )

    def fail(self, payload: Mapping[str, Any]) -> None:
        """Report a terminal failure or hand the clip back to the queue."""
        with self._lock:
            worker_id, root_id, relative_path, _ = self._held_clip(payload)
            error = _optional_str(payload, "error", default=None)
            disposition = _optional_str(payload, "disposition", default="failed")
            if disposition not in {"review", "failed", "requeue"}:
                raise ControllerError(
                    400, "bad_request", "'disposition' must be review, failed, or requeue"
                )
            row = self.store.get_worker(worker_id)
            stopped = bool(row is not None and row.stop_requested)
            if disposition == "requeue":
                self.manifest.update_state(
                    root_id, relative_path, JobState.QUEUED, worker_id=None, error=None
                )
                reason = "stopped by operator" if stopped else (error or "worker request")
                message = f"requeued {relative_path} ({reason})"
                level = "info"
            elif disposition == "review":
                self.manifest.update_state(
                    root_id, relative_path, JobState.REVIEW, error=error or "needs review"
                )
                message = f"{relative_path} needs review: {error or 'unspecified'}"
                level = "warning"
            else:
                self.manifest.update_state(
                    root_id, relative_path, JobState.FAILED, error=error or "failed"
                )
                message = f"failed {relative_path}: {error or 'unspecified'}"
                level = "error"
            self._progress.pop(worker_id, None)
            self._clear_stop_after_job(worker_id, row)
            self._event(
                level,
                message,
                worker_id=worker_id,
                root_id=root_id,
                relative_path=relative_path,
            )

    def _held_clip(self, payload: Mapping[str, Any]) -> tuple[str, str, str, ClipRecord]:
        worker_id = _require_str(payload, "worker_id")
        root_id = _require_str(payload, "root_id")
        relative_path = _require_str(payload, "relative_path")
        record = self._find_clip(root_id, relative_path)
        if record is None:
            raise ControllerError(404, "unknown_job", f"no job for {root_id}:{relative_path}")
        if record.worker_id != worker_id or record.state not in LEASE_STATES:
            raise ControllerError(
                409,
                "lease_conflict",
                f"worker {worker_id!r} does not hold {root_id}:{relative_path}",
            )
        return worker_id, root_id, relative_path, record

    def _template_for(self, record: ClipRecord) -> tuple[str, TemplateSettings]:
        template_id = record.template_id or DEFAULT_TEMPLATE_NAME
        return template_id, self.templates.get(template_id, TemplateSettings())

    def _effective_settings(
        self, template: TemplateSettings, record: ClipRecord
    ) -> dict[str, Any]:
        """Fill the template's settings into the job; the bitrate is not per-clip."""
        return {
            "output_height": template.output_height,
            "video_bitrate_kbps": template.video_bitrate_kbps,
            "audio_bitrate_kbps": template.audio_bitrate_kbps,
            "ffmpeg_preset": template.ffmpeg_preset,
        }

    def _apply_phase(self, record: ClipRecord, progress: object) -> None:
        if not isinstance(progress, Mapping):
            return
        phase = progress.get("phase")
        if not isinstance(phase, str):
            return
        target = _PHASE_STATES.get(phase.lower())
        if target is None or target not in LEASE_STATES or target is record.state:
            return
        if _STAGE_ORDER.index(target) <= _STAGE_ORDER.index(record.state):
            return
        self.manifest.update_state(record.root_id, record.relative_path, target)

    def _finish_job(self, worker_id: str) -> None:
        self._progress.pop(worker_id, None)
        row = self.store.get_worker(worker_id)
        self._clear_stop_after_job(worker_id, row)

    def _clear_stop_after_job(self, worker_id: str, row: WorkerRow | None) -> None:
        if row is not None and row.stop_requested:
            self.store.set_stop_requested(worker_id, False)
            self._event("info", f"stop cleared for {worker_id}", worker_id=worker_id)
