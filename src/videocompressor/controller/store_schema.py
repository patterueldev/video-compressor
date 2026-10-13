"""Controller-store DDL, schema migration, and row codecs (issue #74).

``controller/store.py`` keeps the transactional registry, event, and run
operations; this module owns the ``workers``/``events``/``runs`` DDL, the
idempotent column migration that adds the worker stop-mode fields for existing
databases, and the conversions between SQLite rows and the row dataclasses.

Extracting the DDL and row codec is the seam ``docs/architecture.md``
recommends for keeping the store within its shrink-only size budget, mirroring
``core/manifest_schema.py`` for the queue manifest.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from videocompressor.core.models import StopMode

__all__ = [
    "SCHEMA",
    "EventRow",
    "RunRow",
    "WorkerRow",
    "event_from_row",
    "from_iso",
    "migrate",
    "run_from_row",
    "to_iso",
    "worker_from_row",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS workers (
    worker_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    roots_json TEXT NOT NULL,
    scratch_dir TEXT,
    thread_cap INTEGER NOT NULL,
    version TEXT,
    last_seen TEXT,
    stop_requested INTEGER NOT NULL DEFAULT 0,
    stop_mode TEXT,
    machine_id TEXT,
    registered_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    worker_id TEXT,
    root_id TEXT,
    relative_path TEXT,
    run_id TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    completed INTEGER,
    skipped INTEGER,
    failed INTEGER,
    review INTEGER
);
"""

#: Columns added after the original workers schema: (name, SQL type/constraint).
_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (("stop_mode", "TEXT"), ("machine_id", "TEXT"))

#: Columns listed by the worker-row codec, in :func:`worker_from_row` order.
WORKER_COLUMNS = (
    "worker_id, name, roots_json, scratch_dir, thread_cap, version,"
    " last_seen, stop_requested, stop_mode, registered_at, machine_id"
)

#: Columns listed by the event-row codec, in :func:`event_from_row` order.
EVENT_COLUMNS = "seq, at, level, message, worker_id, root_id, relative_path, run_id"

#: Columns listed by the run-row codec, in :func:`run_from_row` order.
RUN_COLUMNS = "run_id, started_at, ended_at, completed, skipped, failed, review"


@dataclass(frozen=True)
class WorkerRow:
    """One registered worker node."""

    worker_id: str
    name: str
    roots: dict[str, str]
    scratch_dir: str | None
    thread_cap: int
    version: str | None
    last_seen: datetime | None
    stop_requested: bool
    registered_at: datetime
    stop_mode: StopMode | None = None
    machine_id: str | None = None


@dataclass(frozen=True)
class EventRow:
    """One append-only run-log event."""

    seq: int
    at: datetime
    level: str
    message: str
    worker_id: str | None = None
    root_id: str | None = None
    relative_path: str | None = None
    run_id: str | None = None


@dataclass(frozen=True)
class RunRow:
    """One processing run and, once finished, its summary counts."""

    run_id: str
    started_at: datetime
    ended_at: datetime | None
    completed: int | None
    skipped: int | None
    failed: int | None
    review: int | None


def migrate(connection: sqlite3.Connection) -> None:
    """Add columns introduced after the original schema (idempotent)."""
    existing = {row[1] for row in connection.execute("PRAGMA table_info(workers)")}
    for name, definition in _ADDED_COLUMNS:
        if name not in existing:
            connection.execute(f"ALTER TABLE workers ADD COLUMN {name} {definition}")


def to_iso(moment: datetime) -> str:
    """Serialize a datetime for a text column."""
    return moment.isoformat()


def from_iso(value: str | None) -> datetime | None:
    """Parse an ISO-8601 datetime; ``None`` stays ``None``."""
    return datetime.fromisoformat(value) if value is not None else None


def worker_from_row(row: tuple) -> WorkerRow:
    """Convert one workers row (see :data:`WORKER_COLUMNS`) into a row object."""
    raw_mode = row[8]
    return WorkerRow(
        worker_id=row[0],
        name=row[1],
        roots=json.loads(row[2]),
        scratch_dir=row[3],
        thread_cap=row[4],
        version=row[5],
        last_seen=from_iso(row[6]),
        stop_requested=bool(row[7]),
        stop_mode=StopMode(raw_mode) if raw_mode else None,
        registered_at=datetime.fromisoformat(row[9]),
        machine_id=row[10],
    )


def event_from_row(row: tuple) -> EventRow:
    """Convert one events row (see :data:`EVENT_COLUMNS`) into a row object."""
    return EventRow(
        seq=row[0],
        at=datetime.fromisoformat(row[1]),
        level=row[2],
        message=row[3],
        worker_id=row[4],
        root_id=row[5],
        relative_path=row[6],
        run_id=row[7],
    )


def run_from_row(row: tuple) -> RunRow:
    """Convert one runs row (see :data:`RUN_COLUMNS`) into a row object."""
    return RunRow(
        run_id=row[0],
        started_at=datetime.fromisoformat(row[1]),
        ended_at=from_iso(row[2]),
        completed=row[3],
        skipped=row[4],
        failed=row[5],
        review=row[6],
    )
