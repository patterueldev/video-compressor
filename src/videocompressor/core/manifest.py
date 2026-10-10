"""Central manifest: clip records, job states, leases, and the run log.

The SQLite implementation of the interface frozen in issue #1. The controller
(GUI process) owns the durable queue; workers see jobs through the controller
HTTP contract. Claiming is transactional, leases expire so that a disappeared
worker's clip becomes recoverable, and the ``events`` table backs the
controller's run-log endpoint.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Self
from urllib.parse import quote

from videocompressor.core.models import (
    CaptureIssue,
    CaptureTime,
    CaptureTimeMode,
    Classification,
    ClipRecord,
    Fingerprint,
    JobState,
    RunEvent,
)

#: Lease lifetime and heartbeat cadence from docs/architecture.md (issue #7).
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

_SCHEMA = """
CREATE TABLE IF NOT EXISTS clips (
    root_id           TEXT NOT NULL,
    relative_path     TEXT NOT NULL,
    state             TEXT NOT NULL,
    classification    TEXT NOT NULL,
    source_size_bytes INTEGER NOT NULL,
    source_mtime_ns   INTEGER NOT NULL,
    source_digest     TEXT NOT NULL,
    output_size_bytes INTEGER,
    output_mtime_ns   INTEGER,
    output_digest     TEXT,
    capture_time_json TEXT,
    template_id       TEXT,
    settings_hash     TEXT,
    worker_id         TEXT,
    lease_expires_at  TEXT,
    error             TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    completed_at      TEXT,
    PRIMARY KEY (root_id, relative_path)
);

CREATE TABLE IF NOT EXISTS events (
    seq           INTEGER PRIMARY KEY AUTOINCREMENT,
    at            TEXT NOT NULL,
    level         TEXT NOT NULL,
    message       TEXT NOT NULL,
    root_id       TEXT,
    relative_path TEXT
);
"""

_ALLOWED_CHANGES = frozenset(
    {
        "classification",
        "capture_time",
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


def _to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def _from_iso(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value)


def _capture_to_json(capture: CaptureTime | None) -> str | None:
    if capture is None:
        return None
    payload = {
        "when": _to_iso(capture.when),
        "raw": capture.raw,
        "source": capture.source,
        "mode": str(capture.mode),
        "issue": str(capture.issue) if capture.issue is not None else None,
    }
    return json.dumps(payload, sort_keys=True)


def _capture_from_json(payload: str | None) -> CaptureTime | None:
    if payload is None:
        return None
    data = json.loads(payload)
    issue = data.get("issue")
    return CaptureTime(
        when=_from_iso(data.get("when")),
        raw=data.get("raw"),
        source=data.get("source"),
        mode=CaptureTimeMode(data.get("mode", CaptureTimeMode.LOCAL_WALL_CLOCK)),
        issue=CaptureIssue(issue) if issue is not None else None,
    )


def _fingerprint_from_row(row: sqlite3.Row, prefix: str) -> Fingerprint | None:
    size = row[f"{prefix}_size_bytes"]
    if size is None:
        return None
    return Fingerprint(
        size_bytes=size,
        mtime_ns=row[f"{prefix}_mtime_ns"],
        digest=row[f"{prefix}_digest"],
    )


def _row_to_record(row: sqlite3.Row) -> ClipRecord:
    return ClipRecord(
        root_id=row["root_id"],
        relative_path=row["relative_path"],
        source=Fingerprint(
            size_bytes=row["source_size_bytes"],
            mtime_ns=row["source_mtime_ns"],
            digest=row["source_digest"],
        ),
        state=JobState(row["state"]),
        classification=Classification(row["classification"]),
        capture_time=_capture_from_json(row["capture_time_json"]),
        template_id=row["template_id"],
        settings_hash=row["settings_hash"],
        worker_id=row["worker_id"],
        output=_fingerprint_from_row(row, "output"),
        error=row["error"],
        created_at=_from_iso(row["created_at"]),
        updated_at=_from_iso(row["updated_at"]),
        completed_at=_from_iso(row["completed_at"]),
    )


class Manifest:
    """Durable clip/job store shared by the controller, workers, and the GUI."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    @classmethod
    def open(cls, db_path: Path, *, create: bool = True) -> Self:
        """Open (or create) the manifest database at ``db_path``.

        ``create=False`` opens strictly read-only and never modifies the file;
        callers use it for dry runs.
        """
        if create:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(db_path)
        else:
            connection = sqlite3.connect(f"file:{quote(str(db_path))}?mode=ro", uri=True)
        connection.isolation_level = None  # explicit transactions only
        connection.row_factory = sqlite3.Row
        if create:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=5000")
            connection.executescript(_SCHEMA)
        return cls(connection)

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
            """
            INSERT INTO clips (
                root_id, relative_path, state, classification,
                source_size_bytes, source_mtime_ns, source_digest,
                output_size_bytes, output_mtime_ns, output_digest,
                capture_time_json, template_id, settings_hash, worker_id, error,
                created_at, updated_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(root_id, relative_path) DO UPDATE SET
                state = excluded.state,
                classification = excluded.classification,
                source_size_bytes = excluded.source_size_bytes,
                source_mtime_ns = excluded.source_mtime_ns,
                source_digest = excluded.source_digest,
                output_size_bytes = excluded.output_size_bytes,
                output_mtime_ns = excluded.output_mtime_ns,
                output_digest = excluded.output_digest,
                capture_time_json = excluded.capture_time_json,
                template_id = excluded.template_id,
                settings_hash = excluded.settings_hash,
                worker_id = excluded.worker_id,
                error = excluded.error,
                updated_at = excluded.updated_at,
                completed_at = excluded.completed_at
            """,
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
                _capture_to_json(record.capture_time),
                record.template_id,
                record.settings_hash,
                record.worker_id,
                record.error,
                _to_iso(record.created_at) or _to_iso(now),
                _to_iso(record.updated_at) or _to_iso(now),
                _to_iso(record.completed_at),
            ),
        )

    def get_clip(self, root_id: str, relative_path: str) -> ClipRecord | None:
        """Return the record for one clip, or ``None`` when it is unknown."""
        row = self._connection.execute(
            "SELECT * FROM clips WHERE root_id = ? AND relative_path = ?",
            (root_id, relative_path),
        ).fetchone()
        return _row_to_record(row) if row is not None else None

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
        return [_row_to_record(row) for row in rows]

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
                        _to_iso(now + timedelta(seconds=LEASE_TTL_SECONDS)),
                        _to_iso(now),
                        row["root_id"],
                        row["relative_path"],
                        str(JobState.QUEUED),
                    ),
                )
                if cursor.rowcount == 1:
                    claimed_key = (row["root_id"], row["relative_path"])
        return self.get_clip(*claimed_key) if claimed_key is not None else None

    def heartbeat(self, root_id: str, relative_path: str, worker_id: str) -> None:
        """Renew the lease on a claimed clip (90 s TTL, 15 s heartbeat; issue #7)."""
        now = _utcnow()
        cursor = self._connection.execute(
            "UPDATE clips SET lease_expires_at = ?, updated_at = ? "
            "WHERE root_id = ? AND relative_path = ? AND worker_id = ?",
            (
                _to_iso(now + timedelta(seconds=LEASE_TTL_SECONDS)),
                _to_iso(now),
                root_id,
                relative_path,
                worker_id,
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
        record = _row_to_record(row)
        now = _utcnow()

        values: dict[str, object] = {
            "classification": record.classification,
            "capture_time": record.capture_time,
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
                lease_expires_at = _to_iso(now + timedelta(seconds=LEASE_TTL_SECONDS))
        else:
            lease_expires_at = None

        output = values["output"]
        if output is not None and not isinstance(output, Fingerprint):
            raise ValueError("output must be a Fingerprint")
        self._connection.execute(
            """
            UPDATE clips SET
                state = ?, classification = ?, source_size_bytes = ?, source_mtime_ns = ?,
                source_digest = ?, output_size_bytes = ?, output_mtime_ns = ?,
                output_digest = ?, capture_time_json = ?, template_id = ?, settings_hash = ?,
                worker_id = ?, lease_expires_at = ?, error = ?, updated_at = ?,
                completed_at = ?
            WHERE root_id = ? AND relative_path = ?
            """,
            (
                str(state),
                str(values["classification"]),
                values["source"].size_bytes,
                values["source"].mtime_ns,
                values["source"].digest,
                output.size_bytes if output is not None else None,
                output.mtime_ns if output is not None else None,
                output.digest if output is not None else None,
                _capture_to_json(values["capture_time"]),
                values["template_id"],
                values["settings_hash"],
                values["worker_id"],
                lease_expires_at,
                values["error"],
                _to_iso(now),
                _to_iso(values["completed_at"]),
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
                expires = _from_iso(row["lease_expires_at"])
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
                            _to_iso(now),
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
                            _to_iso(now),
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
            (_to_iso(at), level, message, root_id, relative_path),
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
            at = _from_iso(row["at"])
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
