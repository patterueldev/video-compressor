"""SQLite-backed controller state: worker registry, run log, runs, and stop flags.

The durable queue itself lives in the manifest (``core/manifest.py``, issue #3);
this store keeps the controller's own bookkeeping so it survives restarts. A
single connection guarded by a lock is plenty for one GUI process and its
workers. The DDL, the column migration (issue #74), and the row codecs live in
``controller/store_schema.py``; this module keeps the transactional operations.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path

from videocompressor.controller.constants import Clock, system_now
from videocompressor.controller.store_schema import (
    EVENT_COLUMNS,
    RUN_COLUMNS,
    SCHEMA,
    WORKER_COLUMNS,
    EventRow,
    RunRow,
    WorkerRow,
    event_from_row,
    migrate,
    run_from_row,
    to_iso,
    worker_from_row,
)
from videocompressor.core.config import default_config_path
from videocompressor.core.models import StopMode

__all__ = ["ControllerStore", "EventRow", "RunRow", "WorkerRow", "default_db_path"]


def default_db_path() -> Path:
    """Default controller database path (beside the per-user config file)."""
    return default_config_path().parent / "controller.sqlite"


class ControllerStore:
    """Durable controller bookkeeping (registry, events, runs, stop flags)."""

    def __init__(self, conn: sqlite3.Connection, *, clock: Clock = system_now) -> None:
        self._conn = conn
        self._clock = clock
        self._lock = threading.RLock()
        self._closed = False

    @classmethod
    def open(cls, db_path: Path, *, clock: Clock = system_now) -> ControllerStore:
        """Open (or create) the controller database at ``db_path``."""
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        migrate(conn)
        conn.commit()
        return cls(conn, clock=clock)

    def close(self) -> None:
        """Close the database; safe to call more than once."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._conn.close()

    def now(self) -> datetime:
        """Current time according to the store clock."""
        return self._clock()

    # -- worker registry ---------------------------------------------------

    def upsert_worker(
        self,
        worker_id: str,
        *,
        name: str = "",
        roots: dict[str, str] | None = None,
        scratch_dir: str | None = None,
        thread_cap: int = 2,
        version: str | None = None,
    ) -> WorkerRow:
        """Register ``worker_id`` (or refresh it) and clear any pending stop."""
        now = to_iso(self._clock())
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO workers (worker_id, name, roots_json, scratch_dir, thread_cap,
                                     version, last_seen, stop_requested, stop_mode,
                                     registered_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL, ?)
                ON CONFLICT(worker_id) DO UPDATE SET
                    name = excluded.name,
                    roots_json = excluded.roots_json,
                    scratch_dir = excluded.scratch_dir,
                    thread_cap = excluded.thread_cap,
                    version = excluded.version,
                    last_seen = excluded.last_seen,
                    stop_requested = 0,
                    stop_mode = NULL
                """,
                (
                    worker_id,
                    name,
                    json.dumps(roots or {}, sort_keys=True),
                    scratch_dir,
                    thread_cap,
                    version,
                    now,
                    now,
                ),
            )
            self._conn.commit()
            row = self.get_worker(worker_id)
        assert row is not None  # just inserted
        return row

    def get_worker(self, worker_id: str) -> WorkerRow | None:
        """Return the registry entry for ``worker_id``, or None."""
        with self._lock:
            cursor = self._conn.execute(
                f"SELECT {WORKER_COLUMNS} FROM workers WHERE worker_id = ?", (worker_id,)
            )
            row = cursor.fetchone()
        return worker_from_row(row) if row is not None else None

    def list_workers(self) -> list[WorkerRow]:
        """List the registry, ordered by registration time."""
        with self._lock:
            cursor = self._conn.execute(
                f"SELECT {WORKER_COLUMNS} FROM workers ORDER BY registered_at, worker_id"
            )
            rows = cursor.fetchall()
        return [worker_from_row(row) for row in rows]

    def touch_worker(self, worker_id: str) -> WorkerRow | None:
        """Mark ``worker_id`` as seen now; returns None when unregistered."""
        now = to_iso(self._clock())
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE workers SET last_seen = ? WHERE worker_id = ?", (now, worker_id)
            )
            self._conn.commit()
            if cursor.rowcount == 0:
                return None
            return self.get_worker(worker_id)

    def set_stop_requested(
        self, worker_id: str, requested: bool, *, mode: StopMode | None = None
    ) -> WorkerRow | None:
        """Set or clear the stop request for ``worker_id``; None when unregistered.

        Setting ``requested`` stores ``mode`` (default :class:`StopMode.CANCEL`);
        clearing it always clears the mode too.
        """
        stored_mode = (mode or StopMode.CANCEL).value if requested else None
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE workers SET stop_requested = ?, stop_mode = ? WHERE worker_id = ?",
                (int(requested), stored_mode, worker_id),
            )
            self._conn.commit()
            if cursor.rowcount == 0:
                return None
            return self.get_worker(worker_id)

    # -- run log -----------------------------------------------------------

    def append_event(
        self,
        level: str,
        message: str,
        *,
        worker_id: str | None = None,
        root_id: str | None = None,
        relative_path: str | None = None,
        run_id: str | None = None,
    ) -> EventRow:
        """Append one run-log event and return it with its sequence number."""
        at = self._clock()
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO events (at, level, message, worker_id, root_id, relative_path,"
                " run_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    to_iso(at),
                    level,
                    message,
                    worker_id,
                    root_id,
                    relative_path,
                    run_id,
                ),
            )
            self._conn.commit()
            return EventRow(
                seq=int(cursor.lastrowid),  # type: ignore[arg-type]
                at=at,
                level=level,
                message=message,
                worker_id=worker_id,
                root_id=root_id,
                relative_path=relative_path,
                run_id=run_id,
            )

    def list_events(self, *, after: int | None = None, limit: int = 200) -> list[EventRow]:
        """List events, oldest first.

        With ``after``, only events with a higher sequence are returned. Without
        it, the most recent ``limit`` events are returned.
        """
        with self._lock:
            if after is None:
                cursor = self._conn.execute(
                    f"SELECT {EVENT_COLUMNS} FROM events ORDER BY seq DESC LIMIT ?", (limit,)
                )
                rows = list(cursor.fetchall())
                rows.reverse()
            else:
                cursor = self._conn.execute(
                    f"SELECT {EVENT_COLUMNS} FROM events WHERE seq > ? ORDER BY seq LIMIT ?",
                    (after, limit),
                )
                rows = list(cursor.fetchall())
        return [event_from_row(row) for row in rows]

    # -- runs --------------------------------------------------------------

    def start_run(self, run_id: str | None = None) -> RunRow:
        """Create a new run and return it."""
        row_id = run_id or uuid.uuid4().hex[:12]
        with self._lock:
            self._conn.execute(
                "INSERT INTO runs (run_id, started_at) VALUES (?, ?)",
                (row_id, to_iso(self._clock())),
            )
            self._conn.commit()
            row = self.get_run(row_id)
        assert row is not None  # just inserted
        return row

    def finish_run(
        self,
        run_id: str,
        *,
        completed: int,
        skipped: int,
        failed: int,
        review: int,
    ) -> RunRow:
        """Persist the summary counts for an unfinished run."""
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE runs SET ended_at = ?, completed = ?, skipped = ?, failed = ?, review = ?"
                " WHERE run_id = ? AND ended_at IS NULL",
                (to_iso(self._clock()), completed, skipped, failed, review, run_id),
            )
            self._conn.commit()
            if cursor.rowcount == 0:
                raise KeyError(f"run {run_id!r} is unknown or already finished")
            row = self.get_run(run_id)
        assert row is not None
        return row

    def get_run(self, run_id: str) -> RunRow | None:
        """Return one run, or None."""
        with self._lock:
            cursor = self._conn.execute(
                f"SELECT {RUN_COLUMNS} FROM runs WHERE run_id = ?", (run_id,)
            )
            row = cursor.fetchone()
        return run_from_row(row) if row is not None else None

    def active_run(self) -> RunRow | None:
        """Return the newest unfinished run, or None."""
        with self._lock:
            cursor = self._conn.execute(
                f"SELECT {RUN_COLUMNS} FROM runs WHERE ended_at IS NULL"
                " ORDER BY started_at DESC, run_id DESC LIMIT 1"
            )
            row = cursor.fetchone()
        return run_from_row(row) if row is not None else None
