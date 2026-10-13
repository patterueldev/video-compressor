"""Central manifest: clip records, job states, leases, and the run log.

The SQLite implementation of the interface frozen in issue #1. The controller
(GUI process) owns the durable queue; workers see jobs through the controller
HTTP contract. Claiming is transactional, leases expire so that a disappeared
worker's clip becomes recoverable, and the ``events`` table backs the
controller's run-log endpoint.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Self
from urllib.parse import quote

from videocompressor.core.manifest_schema import (
    SCHEMA,
    UPDATE_CLIP_SQL,
    UPSERT_CLIP_SQL,
    capture_to_json,
    destination_to_json,
    from_iso,
    migrate,
    row_to_record,
    to_iso,
)
from videocompressor.core.models import (
    Classification,
    ClipRecord,
    Destination,
    Fingerprint,
    JobState,
    RunEvent,
)

#: Default lease lifetime and heartbeat cadence from docs/architecture.md (issue #7).
#: The controller's ``[controller] orphan_grace_seconds`` setting overrides the TTL (issue #75).
LEASE_TTL_SECONDS = 90
HEARTBEAT_INTERVAL_SECONDS = 15

#: States in which a worker holds a lease on a clip.
LEASE_STATES = frozenset(
    {
        JobState.CLAIMED,
        JobState.COPIED,
        JobState.ENCODING,
        JobState.VALIDATED,
        JobState.TRANSFERRING,
        JobState.REPLACING,
    }
)

#: States whose lease expiry is safe to retry: the source is untouched.
RETRY_STATES = frozenset({JobState.CLAIMED, JobState.COPIED, JobState.ENCODING})

#: States where a replacement may already be in flight; recovery (issue #5)
#: must reconcile the source before the clip can run again.
REPLACEMENT_STATES = frozenset({JobState.VALIDATED, JobState.TRANSFERRING, JobState.REPLACING})

_ALLOWED_CHANGES = frozenset(
    {
        "classification",
        "capture_time",
        "destination",
        "delete_original",
        "template_id",
        "settings_hash",
        "worker_id",
        "output",
        "source",
        "error",
        "completed_at",
    }
)


class LeaseError(RuntimeError):
    """Raised when a worker does not hold the lease it is asking about."""


def _utcnow() -> datetime:
    """Current time; patched by tests to drive lease and recovery behavior."""
    return datetime.now(UTC)


class Manifest:
    """Durable clip/job store shared by the controller, workers, and the GUI."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        lease_ttl_seconds: int = LEASE_TTL_SECONDS,
    ) -> None:
        self._connection = connection
        self._lease_ttl = lease_ttl_seconds

    @classmethod
    def open(
        cls,
        db_path: Path,
        *,
        create: bool = True,
        lease_ttl_seconds: int = LEASE_TTL_SECONDS,
    ) -> Self:
        """Open (or create) the manifest database at ``db_path``.

        ``create=False`` opens strictly read-only and never modifies the file;
        callers use it for dry runs. ``lease_ttl_seconds`` is the job-lease
        lifetime; the controller threads its orphan grace in (issue #75).
        """
        if create:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            # The controller serves HTTP in worker threads and the GUI hosts it
            # in-process; concurrent access is serialized by the caller's lock.
            connection = sqlite3.connect(db_path, check_same_thread=False)
        else:
            connection = sqlite3.connect(f"file:{quote(str(db_path))}?mode=ro", uri=True)
        connection.isolation_level = None  # explicit transactions only
        connection.row_factory = sqlite3.Row
        if create:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=5000")
            connection.executescript(SCHEMA)
            migrate(connection)
        return cls(connection, lease_ttl_seconds=lease_ttl_seconds)

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Cursor]:
        cursor = self._connection.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        try:
            yield cursor
        except BaseException:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()
        finally:
            cursor.close()

    def upsert_clip(self, record: ClipRecord) -> None:
        """Insert or update the record for one clip, keyed by root + relative path."""
        now = _utcnow()
        output = record.output
        self._connection.execute(
            UPSERT_CLIP_SQL,
            (
                record.root_id,
                record.relative_path,
                str(record.state),
                str(record.classification),
                record.source.size_bytes,
                record.source.mtime_ns,
                record.source.digest,
                output.size_bytes if output is not None else None,
                output.mtime_ns if output is not None else None,
                output.digest if output is not None else None,
                capture_to_json(record.capture_time),
                destination_to_json(record.destination),
                int(record.delete_original),
                record.template_id,
                record.settings_hash,
                record.worker_id,
                record.error,
                to_iso(record.created_at) or to_iso(now),
                to_iso(record.updated_at) or to_iso(now),
                to_iso(record.completed_at),
            ),
        )

    def get_clip(self, root_id: str, relative_path: str) -> ClipRecord | None:
        """Return the record for one clip, or ``None`` when it is unknown."""
        row = self._connection.execute(
            "SELECT * FROM clips WHERE root_id = ? AND relative_path = ?",
            (root_id, relative_path),
        ).fetchone()
        return row_to_record(row) if row is not None else None

    def list_clips(self, *, state: JobState | None = None) -> list[ClipRecord]:
        """List clip records, optionally filtered by state."""
        if state is None:
            rows = self._connection.execute(
                "SELECT * FROM clips ORDER BY created_at, relative_path"
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT * FROM clips WHERE state = ? ORDER BY created_at, relative_path",
                (str(state),),
            ).fetchall()
        return [row_to_record(row) for row in rows]

    def claim_next(self, worker_id: str) -> ClipRecord | None:
        """Transactionally claim the next queued clip for ``worker_id``.

        Returns ``None`` when nothing is claimable. A clip must never be claimable
        by two workers at once (issue #7 race requirement).
        """
        now = _utcnow()
        claimed_key: tuple[str, str] | None = None
        with self._transaction() as cursor:
            row = cursor.execute(
                "SELECT root_id, relative_path FROM clips WHERE state = ? "
                "ORDER BY created_at, relative_path LIMIT 1",
                (str(JobState.QUEUED),),
            ).fetchone()
            if row is not None:
                cursor.execute(
                    "UPDATE clips SET state = ?, worker_id = ?, lease_expires_at = ?, "
                    "updated_at = ? WHERE root_id = ? AND relative_path = ? AND state = ?",
                    (
                        str(JobState.CLAIMED),
                        worker_id,
                        to_iso(now + timedelta(seconds=self._lease_ttl)),
                        to_iso(now),
                        row["root_id"],
                        row["relative_path"],
                        str(JobState.QUEUED),
                    ),
                )
                if cursor.rowcount == 1:
                    claimed_key = (row["root_id"], row["relative_path"])
        return self.get_clip(*claimed_key) if claimed_key is not None else None

    def heartbeat(self, root_id: str, relative_path: str, worker_id: str) -> None:
        """Renew the lease on a claimed clip (lease TTL, 15 s heartbeat; issue #7).

        Only clips in a lease-holding state are renewable; terminal states keep
        ``worker_id`` as history, so a late heartbeat after completion or after
        a review-parking reconciliation must raise instead of re-arming a lease.
        """
        now = _utcnow()
        lease_states = sorted(str(state) for state in LEASE_STATES)
        placeholders = ", ".join("?" for _ in lease_states)
        cursor = self._connection.execute(
            "UPDATE clips SET lease_expires_at = ?, updated_at = ? "
            "WHERE root_id = ? AND relative_path = ? AND worker_id = ? "
            f"AND state IN ({placeholders})",
            (
                to_iso(now + timedelta(seconds=self._lease_ttl)),
                to_iso(now),
                root_id,
                relative_path,
                worker_id,
                *lease_states,
            ),
        )
        if cursor.rowcount != 1:
            raise LeaseError(
                f"worker {worker_id!r} does not hold a lease on {root_id}:{relative_path}"
            )

    def update_state(
        self, root_id: str, relative_path: str, state: JobState, **changes: object
    ) -> None:
        """Move a clip to ``state``; ``changes`` updates other ``ClipRecord`` fields.

        Releasing transitions (``queued``) clear the worker and lease; terminal
        states (``complete``, ``failed``, ``review``) clear the lease and keep
        the worker as history. ``complete`` stamps ``completed_at`` when the
        caller does not provide it.
        """
        unknown = set(changes) - _ALLOWED_CHANGES
        if unknown:
            raise ValueError(f"unsupported clip fields: {', '.join(sorted(unknown))}")
        state = JobState(state)
        row = self._connection.execute(
            "SELECT * FROM clips WHERE root_id = ? AND relative_path = ?",
            (root_id, relative_path),
        ).fetchone()
        if row is None:
            raise KeyError(f"no manifest record for {root_id}:{relative_path}")
        record = row_to_record(row)
        now = _utcnow()

        values: dict[str, object] = {
            "classification": record.classification,
            "capture_time": record.capture_time,
            "destination": record.destination,
            "delete_original": record.delete_original,
            "template_id": record.template_id,
            "settings_hash": record.settings_hash,
            "worker_id": record.worker_id,
            "output": record.output,
            "source": record.source,
            "error": record.error,
            "completed_at": record.completed_at,
        }
        values.update(changes)
        if not isinstance(values["classification"], Classification):
            values["classification"] = Classification(values["classification"])  # type: ignore[arg-type]

        if state is JobState.COMPLETE and values["completed_at"] is None:
            values["completed_at"] = now
        if state is JobState.QUEUED and "worker_id" not in changes:
            values["worker_id"] = None

        if state in LEASE_STATES:
            lease_expires_at = row["lease_expires_at"]
            if lease_expires_at is None and state is JobState.CLAIMED:
                lease_expires_at = to_iso(now + timedelta(seconds=self._lease_ttl))
        else:
            lease_expires_at = None

        output = values["output"]
        if output is not None and not isinstance(output, Fingerprint):
            raise ValueError("output must be a Fingerprint")
        destination = values["destination"]
        if destination is not None and not isinstance(destination, Destination):
            raise ValueError("destination must be a Destination")
        if not isinstance(values["delete_original"], bool):
            raise ValueError("delete_original must be a bool")
        self._connection.execute(
            UPDATE_CLIP_SQL,
            (
                str(state),
                str(values["classification"]),
                values["source"].size_bytes,
                values["source"].mtime_ns,
                values["source"].digest,
                output.size_bytes if output is not None else None,
                output.mtime_ns if output is not None else None,
                output.digest if output is not None else None,
                capture_to_json(values["capture_time"]),
                destination_to_json(destination),
                int(values["delete_original"]),
                values["template_id"],
                values["settings_hash"],
                values["worker_id"],
                lease_expires_at,
                values["error"],
                to_iso(now),
                to_iso(values["completed_at"]),
                root_id,
                relative_path,
            ),
        )

    def reconcile(self) -> list[ClipRecord]:
        """Recover expired leases; returns records needing operator review (issues #5/#7).

        Expired leases in the copy/encode stages are safe to retry (the source
        is untouched) and go back to ``queued``. Expired leases in the
        replacement stages park the clip in ``review`` so the crash-recovery
        reconciliation (issue #5) must examine source and backup first. While a
        clip is in review, no worker can claim it again.
        """
        now = _utcnow()
        review_keys: list[tuple[str, str]] = []
        with self._transaction() as cursor:
            rows = cursor.execute(
                "SELECT * FROM clips WHERE lease_expires_at IS NOT NULL"
            ).fetchall()
            for row in rows:
                state = JobState(row["state"])
                expires = from_iso(row["lease_expires_at"])
                if state not in LEASE_STATES or expires is None or expires >= now:
                    continue
                key = (row["root_id"], row["relative_path"])
                if state in RETRY_STATES:
                    cursor.execute(
                        "UPDATE clips SET state = ?, worker_id = NULL, lease_expires_at = NULL, "
                        "error = ?, updated_at = ? WHERE root_id = ? AND relative_path = ?",
                        (
                            str(JobState.QUEUED),
                            "requeued after lease expiry; incomplete local encode will restart",
                            to_iso(now),
                            *key,
                        ),
                    )
                else:
                    cursor.execute(
                        "UPDATE clips SET state = ?, lease_expires_at = NULL, error = ?, "
                        "updated_at = ? WHERE root_id = ? AND relative_path = ?",
                        (
                            str(JobState.REVIEW),
                            "lease expired during replacement; "
                            "reconcile the source before retrying",
                            to_iso(now),
                            *key,
                        ),
                    )
                    review_keys.append(key)
        recovered: list[ClipRecord] = []
        for root_id, relative_path in review_keys:
            record = self.get_clip(root_id, relative_path)
            if record is not None:
                recovered.append(record)
        return recovered

    def record_event(
        self,
        level: str,
        message: str,
        *,
        root_id: str | None = None,
        relative_path: str | None = None,
    ) -> RunEvent:
        """Append one run-log event and return it with its sequence number."""
        at = _utcnow()
        cursor = self._connection.execute(
            "INSERT INTO events (at, level, message, root_id, relative_path) "
            "VALUES (?, ?, ?, ?, ?)",
            (to_iso(at), level, message, root_id, relative_path),
        )
        seq = cursor.lastrowid
        assert seq is not None
        return RunEvent(
            seq=seq,
            at=at,
            level=level,
            message=message,
            root_id=root_id,
            relative_path=relative_path,
        )

    def list_events(self, *, after: int | None = None, limit: int = 200) -> list[RunEvent]:
        """List run-log events; without ``after`` the latest bounded events ascend."""
        if after is None:
            rows = self._connection.execute(
                "SELECT * FROM events ORDER BY seq DESC LIMIT ?", (limit,)
            ).fetchall()
            rows = list(reversed(rows))
        else:
            rows = self._connection.execute(
                "SELECT * FROM events WHERE seq > ? ORDER BY seq ASC LIMIT ?",
                (after, limit),
            ).fetchall()
        events = []
        for row in rows:
            at = from_iso(row["at"])
            assert at is not None
            events.append(
                RunEvent(
                    seq=row["seq"],
                    at=at,
                    level=row["level"],
                    message=row["message"],
                    root_id=row["root_id"],
                    relative_path=row["relative_path"],
                )
            )
        return events
