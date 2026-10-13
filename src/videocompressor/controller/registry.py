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
    ONLINE_WINDOW_SECONDS,
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
from videocompressor.core.models import JobState, StopMode


def _stop_mode_value(row: WorkerRow) -> str:
    """Wire value of a pending stop: a legacy null mode means cancel."""
    return (row.stop_mode or StopMode.CANCEL).value


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
            machine_id = _optional_str(payload, "machine_id")
            self.store.upsert_worker(
                worker_id,
                name=name,
                roots=roots,
                scratch_dir=scratch_dir,
                thread_cap=thread_cap,
                version=version,
                machine_id=machine_id,
            )
            self._mark_online(worker_id)
            self._event("info", f"worker {worker_id} registered", worker_id=worker_id)
            return {
                "lease_seconds": self.lease_ttl_seconds,
                "heartbeat_seconds": HEARTBEAT_INTERVAL_SECONDS,
            }

    def heartbeat(self, worker_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Renew the worker lease and the active job lease; returns the stop order."""
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
                if row.stop_requested and self._find_active_clip(worker_id) is not None:
                    # The worker heartbeats without the clip it still holds, so
                    # it dropped the job without reporting: requeue it and
                    # consume the stop. A worker with nothing to drop keeps the
                    # request pending so this heartbeat can deliver it (an idle
                    # stop must not be swallowed).
                    self._ack_stop_without_report(worker_id)
            return {
                "stop": row.stop_requested,
                "stop_mode": _stop_mode_value(row) if row.stop_requested else None,
                "lease_seconds": self.lease_ttl_seconds,
            }

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
                        "machine_id": row.machine_id,
                        "stop_requested": row.stop_requested,
                        "stop_mode": _stop_mode_value(row) if row.stop_requested else None,
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

    def stop_worker(self, worker_id: str, *, mode: StopMode = StopMode.CANCEL) -> None:
        """Record a node stop request for delivery on the next heartbeat.

        ``cancel`` stops now (cancelling the active job, which requeues);
        ``drain`` lets the active job settle and exits without claiming again.
        Both modes also apply to an idle node, and a later request with another
        mode replaces a pending one.
        """
        with self._lock:
            row = self.store.get_worker(worker_id)
            if row is None:
                raise _unknown_worker(worker_id)
            if row.stop_requested and row.stop_mode == mode:
                return  # already pending, nothing new to deliver
            active = self._find_active_clip(worker_id)
            self.store.set_stop_requested(worker_id, True, mode=mode)
            detail = (
                f"active: {active.relative_path}" if active is not None else "no active job"
            )
            self._event(
                "info",
                f"stop requested for {worker_id} ({mode.value}; {detail})",
                worker_id=worker_id,
                root_id=active.root_id if active is not None else None,
                relative_path=active.relative_path if active is not None else None,
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
        window = min(self.lease_ttl_seconds, ONLINE_WINDOW_SECONDS)
        return (now - row.last_seen).total_seconds() <= window

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
