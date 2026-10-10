"""In-memory ``Manifest`` used until issue #3's SQLite implementation lands.

Implements the frozen API from ``core/manifest.py`` with dicts: nothing survives
a restart. ``ControllerServer`` prefers the SQLite manifest and falls back to
this stub (with a warning) while ``Manifest.open`` is still unimplemented.

Lease semantics follow ``docs/architecture.md``: a claim starts a 90 s lease,
``heartbeat`` renews it, and ``reconcile`` recovers expired claims — requeueing
jobs that never touched the source and parking anything that expired during
``replacing`` in ``review`` until the issue #5 recovery protocol lands.
"""

from __future__ import annotations

import threading
from dataclasses import fields, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Self

from videocompressor.controller.constants import ACTIVE_STATES, LEASE_TTL_SECONDS, Clock, system_now
from videocompressor.core.manifest import Manifest
from videocompressor.core.models import ClipRecord, JobState

_FIELD_NAMES = frozenset(field.name for field in fields(ClipRecord))


class MemoryManifest(Manifest):
    """Thread-safe in-memory implementation of the frozen ``Manifest`` API."""

    def __init__(
        self,
        *,
        clock: Clock = system_now,
        lease_ttl_seconds: int = LEASE_TTL_SECONDS,
    ) -> None:
        self._records: dict[tuple[str, str], ClipRecord] = {}
        self._leases: dict[tuple[str, str], datetime] = {}
        self._clock = clock
        self._lease_ttl = lease_ttl_seconds
        self._lock = threading.RLock()

    @classmethod
    def open(
        cls,
        db_path: Path,
        *,
        clock: Clock = system_now,
        lease_ttl_seconds: int = LEASE_TTL_SECONDS,
    ) -> Self:
        """Return an empty manifest; ``db_path`` is ignored for API compatibility."""
        return cls(clock=clock, lease_ttl_seconds=lease_ttl_seconds)

    # -- frozen Manifest API -----------------------------------------------

    def upsert_clip(self, record: ClipRecord) -> None:
        """Insert or update the record for one clip, keyed by root + relative path."""
        with self._lock:
            self._records[(record.root_id, record.relative_path)] = record

    def list_clips(self, *, state: JobState | None = None) -> list[ClipRecord]:
        """List clip records in insertion order, optionally filtered by state."""
        with self._lock:
            records = list(self._records.values())
        if state is None:
            return records
        return [record for record in records if record.state is state]

    def claim_next(self, worker_id: str) -> ClipRecord | None:
        """Transactionally claim the next queued clip for ``worker_id``."""
        with self._lock:
            for key, record in self._records.items():
                if record.state is not JobState.QUEUED:
                    continue
                now = self._clock()
                claimed = replace(
                    record, state=JobState.CLAIMED, worker_id=worker_id, updated_at=now
                )
                self._records[key] = claimed
                self._leases[key] = now + timedelta(seconds=self._lease_ttl)
                return claimed
            return None

    def heartbeat(self, root_id: str, relative_path: str, worker_id: str) -> None:
        """Renew the lease on a claimed clip; ignores stale or expired leases."""
        with self._lock:
            key = (root_id, relative_path)
            record = self._records.get(key)
            if record is None or record.worker_id != worker_id:
                return
            expires = self._leases.get(key)
            now = self._clock()
            if expires is None or expires <= now:
                return  # expired leases are recovered by reconcile()
            self._leases[key] = now + timedelta(seconds=self._lease_ttl)

    def update_state(
        self, root_id: str, relative_path: str, state: JobState, **changes: object
    ) -> None:
        """Move a clip to ``state``; ``changes`` updates other ``ClipRecord`` fields."""
        unknown = set(changes) - _FIELD_NAMES
        if unknown:
            raise TypeError(f"unknown ClipRecord fields: {sorted(unknown)}")
        with self._lock:
            key = (root_id, relative_path)
            record = self._records.get(key)
            if record is None:
                raise KeyError(f"unknown clip {root_id}:{relative_path}")
            updated = replace(record, state=state, updated_at=self._clock(), **changes)
            self._records[key] = updated
            if state not in ACTIVE_STATES:
                self._leases.pop(key, None)

    def reconcile(self) -> list[ClipRecord]:
        """Recover expired leases; returns every recovered record.

        Records that expired before the replacement stage are requeued; one that
        expired during ``replacing`` parks in ``review`` (recovery required) and
        keeps its worker id for diagnosis. The controller logs each returned
        record by its resulting state.
        """
        recovered: list[ClipRecord] = []
        with self._lock:
            now = self._clock()
            for key, expires in list(self._leases.items()):
                if expires > now:
                    continue
                self._leases.pop(key, None)
                record = self._records.get(key)
                if record is None or record.state not in ACTIVE_STATES:
                    continue
                if record.state is JobState.REPLACING:
                    updated = replace(
                        record,
                        state=JobState.REVIEW,
                        error="lease expired during replacement; recovery required",
                        updated_at=now,
                    )
                else:
                    updated = replace(
                        record,
                        state=JobState.QUEUED,
                        worker_id=None,
                        error=None,
                        updated_at=now,
                    )
                self._records[key] = updated
                recovered.append(updated)
        return recovered
