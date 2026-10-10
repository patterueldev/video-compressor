"""In-memory ``Manifest`` double with the same behavior as the SQLite one.

The SQLite manifest in ``core/manifest.py`` (issue #3) is what production uses:
``vc controller`` opens it through ``open_manifest``. This class stays as a
lightweight, dependency-free double for tests and as the documented fallback
when the SQLite manifest cannot be opened, and mirrors its lease, recovery, and
transactional-claim semantics on plain dicts (nothing survives a restart).
"""

from __future__ import annotations

import threading
from dataclasses import fields, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Self

from videocompressor.controller.constants import LEASE_TTL_SECONDS, Clock, system_now
from videocompressor.core.manifest import (
    LEASE_STATES,
    REPLACEMENT_STATES,
    RETRY_STATES,
    LeaseError,
    Manifest,
)
from videocompressor.core.models import ClipRecord, JobState

_FIELD_NAMES = frozenset(field.name for field in fields(ClipRecord))


class MemoryManifest(Manifest):
    """Thread-safe in-memory stand-in for the SQLite ``Manifest``."""

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

    def get_clip(self, root_id: str, relative_path: str) -> ClipRecord | None:
        """Return the record for one clip, or ``None`` when it is unknown."""
        with self._lock:
            return self._records.get((root_id, relative_path))

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
        """Renew the lease on a lease-holding clip.

        Raises :class:`LeaseError` when the worker does not hold the clip; like
        the SQLite manifest, a renewal also revives a lease that expired but was
        not yet reconciled.
        """
        with self._lock:
            key = (root_id, relative_path)
            record = self._records.get(key)
            if (
                record is None
                or record.worker_id != worker_id
                or record.state not in LEASE_STATES
            ):
                raise LeaseError(
                    f"worker {worker_id!r} does not hold a lease on {root_id}:{relative_path}"
                )
            self._leases[key] = self._clock() + timedelta(seconds=self._lease_ttl)

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
            if state not in LEASE_STATES:
                self._leases.pop(key, None)

    def reconcile(self) -> list[ClipRecord]:
        """Recover expired leases; returns records needing operator review.

        Expired leases in the copy/encode stages (:data:`RETRY_STATES`) are safe
        to retry and go back to ``queued``. Expired leases in the replacement
        stages park the clip in ``review`` (keeping its worker id) so recovery
        can examine source and backup before it runs again. Matching the SQLite
        manifest, only the review records are returned; the controller detects
        requeues from the resulting state changes.
        """
        recovered: list[ClipRecord] = []
        with self._lock:
            now = self._clock()
            for key, expires in list(self._leases.items()):
                if expires > now:
                    continue
                self._leases.pop(key, None)
                record = self._records.get(key)
                if record is None or record.state not in LEASE_STATES:
                    continue
                if record.state in RETRY_STATES:
                    updated = replace(
                        record,
                        state=JobState.QUEUED,
                        worker_id=None,
                        error="requeued after lease expiry; incomplete local encode will restart",
                        updated_at=now,
                    )
                else:
                    assert record.state in REPLACEMENT_STATES
                    updated = replace(
                        record,
                        state=JobState.REVIEW,
                        error="lease expired during replacement; "
                        "reconcile the source before retrying",
                        updated_at=now,
                    )
                    recovered.append(updated)
                self._records[key] = updated
        return recovered
