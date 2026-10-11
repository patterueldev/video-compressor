"""Explicit-selection enqueue mixin for :class:`ControllerServer` (issue #39).

Batch enqueue of explicit files for direct conversions: the batch-level fields
are validated once, then every file becomes a durable ``queued`` manifest
record, with per-file status and a skip/reject reason. Duplicates skip and a
re-enqueue is idempotent; nothing existing is ever rewritten here.

The methods operate on state created by ``ControllerServer.__init__``
(``manifest``, ``templates``, ``_lock``, ``_roots``, ``_fingerprint``,
``_clock``) and on the facade's shared ``_event`` helper. The mixin has no
``__init__`` of its own.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from videocompressor.controller.protocol import ControllerError, _require_str
from videocompressor.core.config import DEFAULT_TEMPLATE_NAME
from videocompressor.core.models import (
    ArchiveRoot,
    Classification,
    ClipRecord,
    Destination,
    DestinationKind,
    JobState,
    destination_from_dict,
    is_safe_relative_path,
)


class JobEnqueueMixin:
    """Batch enqueue of explicit files, returning per-file status."""

    def enqueue(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Queue explicit files; returns per-file results plus summary counts."""
        with self._lock:
            files = _files(payload)
            roots = self._roots_by_id()
            destination = _destination(payload, roots)
            delete_original = _optional_bool(payload, "delete_original", default=False)
            _validate_delete_choice(destination, delete_original)
            template_id = _template_id(payload, self.templates)
            seen: set[tuple[str, str]] = set()
            results: list[dict[str, Any]] = []
            counts = {"queued": 0, "skipped": 0, "rejected": 0}
            for entry in files:
                root_id = _require_str(entry, "root_id")
                relative_path = _require_str(entry, "relative_path")
                status, reason = self._enqueue_file(
                    root_id,
                    relative_path,
                    roots=roots,
                    destination=destination,
                    template_id=template_id,
                    delete_original=delete_original,
                    seen=seen,
                )
                counts[status] += 1
                results.append(_job_result(root_id, relative_path, status, reason))
            self._event(
                "info",
                f"enqueued {counts['queued']} of {len(files)} clips"
                f" ({counts['skipped']} skipped, {counts['rejected']} rejected)",
            )
            return {
                "jobs": results,
                "queued": counts["queued"],
                "skipped": counts["skipped"],
                "rejected": counts["rejected"],
            }

    # -- internals ---------------------------------------------------------

    def _enqueue_file(
        self,
        root_id: str,
        relative_path: str,
        *,
        roots: Mapping[str, ArchiveRoot],
        destination: Destination,
        template_id: str,
        delete_original: bool,
        seen: set[tuple[str, str]],
    ) -> tuple[str, str | None]:
        """Queue one file; returns ``(status, reason)``.

        Dedup runs before any filesystem access so a re-enqueue never touches
        the disk; rejected entries never block the rest of the batch.
        """
        key = (root_id, relative_path)
        if key in seen:
            return "skipped", "duplicate_in_batch"
        seen.add(key)
        reason = _skip_reason(self.manifest.get_clip(root_id, relative_path))
        if reason is not None:
            return "skipped", reason
        root = roots.get(root_id)
        if root is None:
            return "rejected", "unknown_root"
        if not is_safe_relative_path(relative_path):
            return "rejected", "unsafe_relative_path"
        path = root.path.joinpath(*PurePosixPath(relative_path).parts)
        if not _within_root(path, root.path):
            return "rejected", "unsafe_relative_path"
        try:
            source = self._fingerprint(path)
        except OSError:
            return "rejected", "not_a_file" if not path.is_file() else "unreadable"
        now = self._clock()
        self.manifest.upsert_clip(
            ClipRecord(
                root_id=root_id,
                relative_path=relative_path,
                source=source,
                state=JobState.QUEUED,
                classification=Classification.NEW,
                template_id=template_id,
                destination=destination,
                delete_original=delete_original,
                created_at=now,
                updated_at=now,
            )
        )
        return "queued", None

    def _roots_by_id(self) -> dict[str, ArchiveRoot]:
        return {root.id: root for root in self._roots()}


def _files(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Validate the batch shape; every entry is an explicit file."""
    value = payload.get("files")
    if not isinstance(value, list) or not value:
        raise ControllerError(400, "bad_request", "'files' must be a non-empty list")
    files: list[Mapping[str, Any]] = []
    for index, entry in enumerate(value):
        if not isinstance(entry, Mapping):
            raise ControllerError(400, "bad_request", f"files[{index}] must be an object")
        _require_str(entry, "root_id")
        _require_str(entry, "relative_path")
        files.append(entry)
    return files


def _destination(payload: Mapping[str, Any], roots: Mapping[str, ArchiveRoot]) -> Destination:
    """Parse and validate the required batch-level destination spec."""
    value = payload.get("destination")
    if value is None:
        raise ControllerError(400, "bad_request", "'destination' is required")
    try:
        destination = destination_from_dict(value)
    except ValueError as exc:
        raise ControllerError(400, "bad_request", f"invalid destination: {exc}") from None
    assert destination is not None  # guaranteed by the None check above
    if destination.kind is DestinationKind.OUTPUT_ROOT and destination.root_id not in roots:
        raise ControllerError(
            400, "bad_request", f"destination root {destination.root_id!r} is not registered"
        )
    return destination


def _validate_delete_choice(destination: Destination, delete_original: bool) -> None:
    """Refuse the combinations the worker rejects (issue #40 semantics)."""
    if destination.kind is DestinationKind.SAME_FOLDER and not delete_original:
        raise ControllerError(
            400,
            "bad_request",
            "a same-folder output replaces the source; set 'delete_original' to true"
            " or choose another destination",
        )


def _template_id(payload: Mapping[str, Any], templates: Mapping[str, object]) -> str:
    value = payload.get("template_id", DEFAULT_TEMPLATE_NAME)
    if not isinstance(value, str) or not value:
        raise ControllerError(400, "bad_request", "'template_id' must be a non-empty string")
    if value not in templates:
        raise ControllerError(400, "bad_request", f"unknown template {value!r}")
    return value


def _optional_bool(payload: Mapping[str, Any], key: str, *, default: bool) -> bool:
    value = payload.get(key, default)
    if not isinstance(value, bool):
        raise ControllerError(400, "bad_request", f"{key!r} must be a boolean")
    return value


def _skip_reason(record: ClipRecord | None) -> str | None:
    """Why an existing record is not queued again, or None when it is new."""
    if record is None:
        return None
    if record.state is JobState.COMPLETE:
        return "already_complete"
    if record.state is JobState.REVIEW:
        return "needs_review"
    if record.state is JobState.FAILED:
        return "previously_failed"
    return "already_queued"


def _within_root(path: Path, root: Path) -> bool:
    """Whether the resolved candidate stays inside the resolved root."""
    root_resolved = root.resolve()
    resolved = path.resolve()
    return resolved == root_resolved or root_resolved in resolved.parents


def _job_result(
    root_id: str, relative_path: str, status: str, reason: str | None
) -> dict[str, Any]:
    return {
        "job_id": f"{root_id}:{relative_path}",
        "root_id": root_id,
        "relative_path": relative_path,
        "status": status,
        "reason": reason,
    }
