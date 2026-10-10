"""Worker-registry mixin for :class:`ControllerServer`.

Register/heartbeat/stop bookkeeping, moved verbatim from
``controller/server.py`` (issue #21). The methods operate on the state created
by ``ControllerServer.__init__`` (``store``, ``manifest``, ``_lock``,
``_progress``, ``_online``, ``_clock``, ``lease_ttl_seconds``) and on shared
lookups kept on the facade (``_find_clip``, ``_find_active_clip``, ``_event``).
The mixin has no ``__init__`` of its own.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from videocompressor.controller.constants import (
    HEARTBEAT_INTERVAL_SECONDS,
    LEASE_STATES,
    WORKER_ID_PATTERN,
)
from videocompressor.controller.protocol import (
    ControllerError,
    _optional_int,
    _optional_str,
    _optional_str_map,
    _require_str,
    _unknown_worker,
)
from videocompressor.controller.store import WorkerRow
from videocompressor.core.manifest import LeaseError
from videocompressor.core.models import JobState


class WorkerRegistryMixin:
    """Worker registration, heartbeats, and stop/resume flags."""

    def register_worker(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Register or refresh a worker; returns the lease/heartbeat intervals."""
        with self._lock:
            worker_id = _require_str(payload, "worker_id")
            if not WORKER_ID_PATTERN.fullmatch(worker_id):
                raise ControllerError(
                    400, "invalid_worker_id", "worker_id must match [A-Za-z0-9_.-]{1,64}"
                )
            name = _optional_str(payload, "name", default=worker_id) or worker_id
            roots = _optional_str_map(payload, "roots")
            scratch_dir = _optional_str(payload, "scratch_dir")
            thread_cap = _optional_int(payload, "thread_cap", default=2)
            version = _optional_str(payload, "version")
            self.store.upsert_worker(
                worker_id,
                name=name,
                roots=roots,
                scratch_dir=scratch_dir,
                thread_cap=thread_cap,
                version=version,
            )
            self._mark_online(worker_id)
            self._event("info", f"worker {worker_id} registered", worker_id=worker_id)
            return {
                "lease_seconds": self.lease_ttl_seconds,
                "heartbeat_seconds": HEARTBEAT_INTERVAL_SECONDS,
            }

    def heartbeat(self, worker_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Renew the worker lease and the active job lease; returns the stop flag."""
        with self._lock:
            row = self.store.touch_worker(worker_id)
            if row is None:
                raise _unknown_worker(worker_id)
            self._mark_online(worker_id)
            active = payload.get("active")
            progress = payload.get("progress")
            if active is not None:
                if not isinstance(active, Mapping):
                    raise ControllerError(400, "bad_request", "'active' must be an object")
                root_id = _require_str(active, "root_id")
                relative_path = _require_str(active, "relative_path")
                record = self._find_clip(root_id, relative_path)
                if (
                    record is None
                    or record.worker_id != worker_id
                    or record.state not in LEASE_STATES
                ):
                    raise ControllerError(
                        409,
                        "lease_conflict",
                        f"worker {worker_id!r} does not hold {root_id}:{relative_path}",
                    )
                try:
                    self.manifest.heartbeat(root_id, relative_path, worker_id)
                except LeaseError:
                    raise ControllerError(
                        409,
                        "lease_conflict",
                        f"worker {worker_id!r} does not hold {root_id}:{relative_path}",
                    ) from None
                self._progress[worker_id] = {
                    "active": {"root_id": root_id, "relative_path": relative_path},
                    "progress": dict(progress) if isinstance(progress, Mapping) else None,
                    "updated_at": self._clock().isoformat(),
                }
                self._apply_phase(record, progress)
            else:
                self._progress.pop(worker_id, None)
                if row.stop_requested:
                    self._ack_stop_without_report(worker_id)
            current = self.store.get_worker(worker_id)
            stop = bool(current.stop_requested) if current is not None else False
            return {"stop": stop, "lease_seconds": self.lease_ttl_seconds}

    def workers(self) -> dict[str, Any]:
        """Registry snapshot with online state, active clip, and latest progress."""
        with self._lock:
            now = self._clock()
            entries = []
            for row in self.store.list_workers():
                active = self._find_active_clip(row.worker_id)
                cached = self._progress.get(row.worker_id, {})
                entries.append(
                    {
                        "worker_id": row.worker_id,
                        "name": row.name,
                        "online": self._is_online(row, now),
                        "last_seen": row.last_seen.isoformat() if row.last_seen else None,
                        "roots": row.roots,
                        "scratch_dir": row.scratch_dir,
                        "thread_cap": row.thread_cap,
                        "version": row.version,
                        "stop_requested": row.stop_requested,
                        "active": (
                            {"root_id": active.root_id, "relative_path": active.relative_path}
                            if active is not None
                            else None
                        ),
                        "progress": cached.get("progress"),
                        "progress_at": cached.get("updated_at"),
                    }
                )
            return {"workers": entries}

    def stop_worker(self, worker_id: str) -> None:
        """Ask a worker to terminate its active job at the next heartbeat."""
        with self._lock:
            row = self.store.get_worker(worker_id)
            if row is None:
                raise _unknown_worker(worker_id)
            active = self._find_active_clip(worker_id)
            if active is None:
                self._event(
                    "info", f"stop requested for {worker_id}: no active job", worker_id=worker_id
                )
                return
            if not row.stop_requested:
                self.store.set_stop_requested(worker_id, True)
                self._event(
                    "info",
                    f"stop requested for {worker_id} (active: {active.relative_path})",
                    worker_id=worker_id,
                    root_id=active.root_id,
                    relative_path=active.relative_path,
                )

    def resume_worker(self, worker_id: str) -> None:
        """Clear a pending stop request."""
        with self._lock:
            row = self.store.get_worker(worker_id)
            if row is None:
                raise _unknown_worker(worker_id)
            if row.stop_requested:
                self.store.set_stop_requested(worker_id, False)
                self._event("info", f"resumed {worker_id}", worker_id=worker_id)

    def _mark_online(self, worker_id: str) -> None:
        if worker_id not in self._online:
            self._online.add(worker_id)
            self._event("info", f"worker {worker_id} online", worker_id=worker_id)

    def _is_online(self, row: WorkerRow, now: datetime) -> bool:
        if row.last_seen is None:
            return False
        return (now - row.last_seen).total_seconds() <= self.lease_ttl_seconds

    def _ack_stop_without_report(self, worker_id: str) -> None:
        active = self._find_active_clip(worker_id)
        if active is not None:
            self.manifest.update_state(
                active.root_id, active.relative_path, JobState.QUEUED, worker_id=None, error=None
            )
            self._event(
                "warning",
                f"requeued {active.relative_path}: {worker_id} stopped without reporting",
                worker_id=worker_id,
                root_id=active.root_id,
                relative_path=active.relative_path,
            )
        self.store.set_stop_requested(worker_id, False)
        self._event("info", f"stop cleared for {worker_id}", worker_id=worker_id)
