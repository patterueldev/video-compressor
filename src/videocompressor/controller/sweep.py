"""Lease-sweep mixin for :class:`ControllerServer`.

The periodic housekeeping pass that reconciles expired leases and flips silent
workers offline, moved verbatim from ``controller/server.py`` (issue #21). The
methods operate on the state created by ``ControllerServer.__init__`` and on
shared lookups kept on the facade (``_find_active_clip``, ``_is_online``,
``_event``). The mixin has no ``__init__`` of its own.
"""

from __future__ import annotations

import logging

from videocompressor.controller.constants import LEASE_STATES
from videocompressor.core.models import JobState

_log = logging.getLogger("videocompressor.controller")


class LeaseSweepMixin:
    """Expired-lease reconciliation and worker-offline housekeeping."""

    def sweep_once(self) -> None:
        """One housekeeping pass: reconcile expired leases and mark offline workers."""
        with self._lock:
            # Manifest.reconcile() returns the clips parked in review; requeues
            # are detected from the resulting state changes. Snapshot first so
            # the run log can name the worker that held each recovered clip.
            before = {
                (record.root_id, record.relative_path): (record.state, record.worker_id)
                for record in self.manifest.list_clips()
            }
            recovered = self.manifest.reconcile()
            review_keys: set[tuple[str, str]] = set()
            for record in recovered:
                key = (record.root_id, record.relative_path)
                review_keys.add(key)
                prior = before.get(key)
                worker_id = record.worker_id or (prior[1] if prior is not None else None)
                self._event(
                    "warning",
                    f"{record.relative_path} parked in review:"
                    " lease expired during replacement, recovery required",
                    worker_id=worker_id,
                    root_id=record.root_id,
                    relative_path=record.relative_path,
                )
            for record in self.manifest.list_clips():
                key = (record.root_id, record.relative_path)
                prior = before.get(key)
                if (
                    key not in review_keys
                    and record.state is JobState.QUEUED
                    and prior is not None
                    and prior[0] in LEASE_STATES
                ):
                    self._event(
                        "warning",
                        f"requeued {record.relative_path}: lease expired",
                        worker_id=prior[1],
                        root_id=record.root_id,
                        relative_path=record.relative_path,
                    )
            now = self._clock()
            for row in self.store.list_workers():
                if row.worker_id in self._online and not self._is_online(row, now):
                    self._online.discard(row.worker_id)
                    self._event(
                        "warning", f"worker {row.worker_id} offline", worker_id=row.worker_id
                    )
                if row.stop_requested and not self._is_online(row, now):
                    # Only dead registrations are cleaned up here: a pending
                    # stop for an online worker must survive until its next
                    # heartbeat (at most 15 s) delivers it, or an idle stop
                    # would be dropped before the worker ever sees it.
                    self.store.set_stop_requested(row.worker_id, False)

    def _sweep_loop(self) -> None:
        while not self._stopping.wait(self.sweep_interval_seconds):
            try:
                self.sweep_once()
            except Exception:  # keep the controller alive through one bad sweep
                _log.exception("controller sweep failed")
