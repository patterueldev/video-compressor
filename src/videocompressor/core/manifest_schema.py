"""Manifest DDL, DML statements, schema migration, and row codecs (issue #39).

``core/manifest.py`` keeps the transactional queue operations; this module owns
the clips/events DDL, the clip upsert/update statements, the idempotent column
migration that adds the direct-conversion job fields (``destination_json``,
``delete_original``), and the conversions between SQLite rows and the shared
models. Extracting the DDL, the statements, and the row codec is the seam
``docs/architecture.md`` recommends for keeping ``core/manifest.py`` within its
shrink-only size budget.

The row codec tolerates a database opened read-only before the migration ran,
returning the documented defaults for the added columns.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from videocompressor.core.models import (
    CaptureIssue,
    CaptureTime,
    CaptureTimeMode,
    Classification,
    ClipRecord,
    Destination,
    Fingerprint,
    JobState,
    destination_from_dict,
    destination_to_dict,
)

__all__ = [
    "SCHEMA",
    "UPDATE_CLIP_SQL",
    "UPSERT_CLIP_SQL",
    "capture_from_json",
    "capture_to_json",
    "destination_from_json",
    "destination_to_json",
    "fingerprint_from_row",
    "from_iso",
    "migrate",
    "row_to_record",
    "to_iso",
]

SCHEMA = """
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
    destination_json  TEXT,
    delete_original   INTEGER NOT NULL DEFAULT 0,
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

#: Insert or update one clip row, keyed by root + relative path.
UPSERT_CLIP_SQL = """
INSERT INTO clips (
    root_id, relative_path, state, classification,
    source_size_bytes, source_mtime_ns, source_digest,
    output_size_bytes, output_mtime_ns, output_digest,
    capture_time_json, destination_json, delete_original,
    template_id, settings_hash, worker_id, error,
    created_at, updated_at, completed_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    destination_json = excluded.destination_json,
    delete_original = excluded.delete_original,
    template_id = excluded.template_id,
    settings_hash = excluded.settings_hash,
    worker_id = excluded.worker_id,
    error = excluded.error,
    updated_at = excluded.updated_at,
    completed_at = excluded.completed_at
"""

#: Full row update for one clip, keyed by root + relative path.
UPDATE_CLIP_SQL = """
UPDATE clips SET
    state = ?, classification = ?, source_size_bytes = ?, source_mtime_ns = ?,
    source_digest = ?, output_size_bytes = ?, output_mtime_ns = ?,
    output_digest = ?, capture_time_json = ?, destination_json = ?,
    delete_original = ?, template_id = ?, settings_hash = ?,
    worker_id = ?, lease_expires_at = ?, error = ?, updated_at = ?,
    completed_at = ?
WHERE root_id = ? AND relative_path = ?
"""

#: Columns added after the original clips schema: (name, SQL type/constraint).
_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("destination_json", "TEXT"),
    ("delete_original", "INTEGER NOT NULL DEFAULT 0"),
)


def migrate(connection: sqlite3.Connection) -> None:
    """Add columns introduced after the original schema (idempotent)."""
    existing = {row[1] for row in connection.execute("PRAGMA table_info(clips)")}
    for name, definition in _ADDED_COLUMNS:
        if name not in existing:
            connection.execute(f"ALTER TABLE clips ADD COLUMN {name} {definition}")


def to_iso(value: datetime | None) -> str | None:
    """Serialize a datetime, assuming UTC for naive values."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def from_iso(value: str | None) -> datetime | None:
    """Parse an ISO-8601 datetime; ``None`` stays ``None``."""
    if value is None:
        return None
    return datetime.fromisoformat(value)


def capture_to_json(capture: CaptureTime | None) -> str | None:
    """Serialize a capture time for the ``capture_time_json`` column."""
    if capture is None:
        return None
    payload = {
        "when": to_iso(capture.when),
        "raw": capture.raw,
        "source": capture.source,
        "mode": str(capture.mode),
        "issue": str(capture.issue) if capture.issue is not None else None,
    }
    return json.dumps(payload, sort_keys=True)


def capture_from_json(payload: str | None) -> CaptureTime | None:
    """Parse the ``capture_time_json`` column."""
    if payload is None:
        return None
    data = json.loads(payload)
    issue = data.get("issue")
    return CaptureTime(
        when=from_iso(data.get("when")),
        raw=data.get("raw"),
        source=data.get("source"),
        mode=CaptureTimeMode(data.get("mode", CaptureTimeMode.LOCAL_WALL_CLOCK)),
        issue=CaptureIssue(issue) if issue is not None else None,
    )


def destination_to_json(destination: Destination | None) -> str | None:
    """Serialize a destination spec for the ``destination_json`` column."""
    if destination is None:
        return None
    return json.dumps(destination_to_dict(destination), sort_keys=True)


def destination_from_json(payload: str | None) -> Destination | None:
    """Parse the ``destination_json`` column (``None`` for legacy records)."""
    if payload is None:
        return None
    return destination_from_dict(json.loads(payload))


def fingerprint_from_row(row: sqlite3.Row, prefix: str) -> Fingerprint | None:
    """Read a ``<prefix>_{size_bytes,mtime_ns,digest}`` fingerprint, or ``None``."""
    size = row[f"{prefix}_size_bytes"]
    if size is None:
        return None
    return Fingerprint(
        size_bytes=size,
        mtime_ns=row[f"{prefix}_mtime_ns"],
        digest=row[f"{prefix}_digest"],
    )


def row_to_record(row: sqlite3.Row) -> ClipRecord:
    """Convert one clips row into a :class:`ClipRecord`.

    Columns added after the original schema are read defensively so a database
    opened read-only before the migration returns the documented defaults.
    """
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
        capture_time=capture_from_json(row["capture_time_json"]),
        template_id=row["template_id"],
        settings_hash=row["settings_hash"],
        worker_id=row["worker_id"],
        output=fingerprint_from_row(row, "output"),
        error=row["error"],
        created_at=from_iso(row["created_at"]),
        updated_at=from_iso(row["updated_at"]),
        completed_at=from_iso(row["completed_at"]),
        destination=destination_from_json(_optional_column(row, "destination_json", None)),
        delete_original=bool(_optional_column(row, "delete_original", 0)),
    )


def _optional_column(row: sqlite3.Row, name: str, default: Any) -> Any:
    """Value of ``row[name]`` when the column exists, else ``default``."""
    return row[name] if name in row.keys() else default
