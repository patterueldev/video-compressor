"""Shared factories for the monitor tests (issue #9).

No Qt imports here on purpose: the pure monitor suites use these helpers and
keep running without the optional ``gui`` extra.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from videocompressor.gui.monitor_service import (
    JobProgress,
    LogEvent,
    MonitorSnapshot,
    SummaryCounts,
    WorkerStatus,
)

#: Fixed reference instant for snapshots and payloads (aware, UTC).
NOW = datetime(2026, 10, 11, 9, 0, 0, tzinfo=UTC)

#: Clip the active-worker factories hold.
ACTIVE_PATH = "2026-06-Ride/GS010001.MP4"


def make_progress(**overrides: object) -> JobProgress:
    """An encoding heartbeat: pass 2, 41.2 %, 38.5 fps, 0.74x, ETA 20m 34s."""
    fields: dict[str, object] = {
        "phase": "encoding",
        "pass_number": 2,
        "percent": 41.2,
        "fps": 38.5,
        "speed": "0.74x",
        "eta_seconds": 1234.0,
    }
    fields.update(overrides)
    return JobProgress(**fields)  # type: ignore[arg-type]


def make_worker(worker_id: str = "homeserver", **overrides: object) -> WorkerStatus:
    """An online worker mid-encode."""
    fields: dict[str, object] = {
        "worker_id": worker_id,
        "name": worker_id,
        "online": True,
        "last_seen": NOW - timedelta(seconds=12),
        "roots": {"archive": "/srv/archive"},
        "stop_requested": False,
        "active_root_id": "archive",
        "active_path": ACTIVE_PATH,
        "progress": make_progress(),
        "progress_at": NOW - timedelta(seconds=12),
    }
    fields.update(overrides)
    return WorkerStatus(**fields)  # type: ignore[arg-type]


def make_idle_worker(worker_id: str = "macbook", **overrides: object) -> WorkerStatus:
    """An offline worker with no active clip."""
    fields: dict[str, object] = {
        "name": worker_id,
        "online": False,
        "last_seen": NOW - timedelta(minutes=5),
        "roots": {"archive": "/Volumes/archive"},
        "stop_requested": False,
        "active_root_id": None,
        "active_path": None,
        "progress": None,
        "progress_at": None,
    }
    fields.update(overrides)
    return make_worker(worker_id, **fields)


def make_event(seq: int = 1, **overrides: object) -> LogEvent:
    """One run-log event about the active clip."""
    fields: dict[str, object] = {
        "seq": seq,
        "at": NOW - timedelta(seconds=30),
        "level": "info",
        "message": "claimed GS010001.MP4",
        "worker_id": "homeserver",
        "root_id": "archive",
        "relative_path": ACTIVE_PATH,
        "run_id": None,
    }
    fields.update(overrides)
    return LogEvent(**fields)  # type: ignore[arg-type]


def make_summary(**overrides: object) -> SummaryCounts:
    """The plan.md summary counts: completed/skipped/failed/review."""
    fields: dict[str, object] = {"completed": 3, "skipped": 12, "failed": 0, "review": 1}
    fields.update(overrides)
    return SummaryCounts(**fields)  # type: ignore[arg-type]


def make_snapshot(
    workers: list[WorkerStatus] | None = None,
    events: list[LogEvent] | None = None,
    summary: SummaryCounts | None = None,
    *,
    fetched_at: datetime = NOW,
) -> MonitorSnapshot:
    """A complete monitor snapshot with sensible defaults."""
    return MonitorSnapshot(
        workers=tuple(workers) if workers is not None else (make_worker(),),
        events=tuple(events) if events is not None else (make_event(),),
        summary=summary if summary is not None else make_summary(),
        fetched_at=fetched_at,
    )


# -- raw contract payloads (parser and client tests) ------------------------


def worker_payload(worker_id: str = "homeserver", **overrides: object) -> dict[str, Any]:
    """A ``GET /workers`` entry at the contract defaults."""
    payload: dict[str, Any] = {
        "worker_id": worker_id,
        "name": worker_id,
        "online": True,
        "last_seen": (NOW - timedelta(seconds=12)).isoformat(),
        "roots": {"archive": "/srv/archive"},
        "scratch_dir": "/srv/scratch",
        "thread_cap": 2,
        "version": "0.1.0",
        "stop_requested": False,
        "active": {"root_id": "archive", "relative_path": ACTIVE_PATH},
        "progress": {
            "phase": "encoding",
            "pass": 2,
            "percent": 41.2,
            "fps": 38.5,
            "speed": "0.74x",
            "eta_seconds": 1234,
        },
        "progress_at": NOW.isoformat(),
    }
    payload.update(overrides)
    return payload


def workers_payload(*workers: object) -> dict[str, Any]:
    """A ``GET /workers`` body."""
    return {"workers": list(workers)}


def event_payload(seq: int = 1, **overrides: object) -> dict[str, Any]:
    """A ``GET /events`` entry at the contract defaults."""
    payload: dict[str, Any] = {
        "seq": seq,
        "at": NOW.isoformat(),
        "level": "info",
        "message": "claimed GS010001.MP4",
        "worker_id": "homeserver",
        "root_id": "archive",
        "relative_path": ACTIVE_PATH,
    }
    payload.update(overrides)
    return payload


def events_payload(events: list[dict[str, Any]], *, next_seq: int | None = None) -> dict[str, Any]:
    """A ``GET /events`` body; ``next`` defaults to one past the last event."""
    if next_seq is None:
        next_seq = events[-1]["seq"] + 1 if events else 0
    return {"events": list(events), "next": next_seq}


def summary_payload(**overrides: object) -> dict[str, Any]:
    """A ``GET /summary`` body."""
    payload: dict[str, Any] = {
        "completed": 3,
        "skipped": 12,
        "failed": 0,
        "review": 1,
        "run_id": None,
        "generated_at": NOW.isoformat(),
    }
    payload.update(overrides)
    return payload


class FakeMonitorClient:
    """Socket-free stand-in for ``HttpControllerClient`` (session tests)."""

    def __init__(
        self,
        *,
        workers: dict[str, Any] | None = None,
        event_pages: list[dict[str, Any]] | None = None,
        summary: dict[str, Any] | None = None,
    ) -> None:
        self._workers = workers if workers is not None else workers_payload()
        self._event_pages = list(event_pages) if event_pages else [events_payload([])]
        self._summary = summary if summary is not None else summary_payload()
        self.event_calls: list[int | None] = []
        self.stops: list[tuple[str, str]] = []
        self.resumes: list[str] = []
        self.enqueues: list[dict[str, Any]] = []
        self.enqueue_response: dict[str, Any] = {
            "jobs": [],
            "queued": 0,
            "skipped": 0,
            "rejected": 0,
        }
        self.enqueue_error: Exception | None = None

    def workers(self) -> dict[str, Any]:
        return self._workers

    def events(self, *, after: int | None = None, limit: int | None = None) -> dict[str, Any]:
        self.event_calls.append(after)
        if len(self._event_pages) > 1:
            return self._event_pages.pop(0)
        return self._event_pages[0]

    def summary(self, *, run_id: str | None = None) -> dict[str, Any]:
        return self._summary

    def enqueue(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.enqueues.append(payload)
        if self.enqueue_error is not None:
            raise self.enqueue_error
        return self.enqueue_response

    def stop_worker(self, worker_id: str, *, mode: str = "cancel") -> None:
        self.stops.append((worker_id, mode))

    def resume_worker(self, worker_id: str) -> None:
        self.resumes.append(worker_id)


class FakeSession:
    """Stand-in for ``MonitorSession`` (panel tests)."""

    def __init__(
        self,
        snapshot: MonitorSnapshot | None = None,
        *,
        stop_error: Exception | None = None,
        resume_error: Exception | None = None,
    ) -> None:
        self._snapshot = snapshot if snapshot is not None else make_snapshot()
        self.stop_error = stop_error
        self.resume_error = resume_error
        self.stops: list[tuple[str, str]] = []
        self.resumes: list[str] = []
        self.snapshot_calls = 0

    def snapshot(self) -> MonitorSnapshot:
        self.snapshot_calls += 1
        return self._snapshot

    def stop_worker(self, worker_id: str, *, mode: str = "cancel") -> None:
        if self.stop_error is not None:
            raise self.stop_error
        self.stops.append((worker_id, mode))

    def resume_worker(self, worker_id: str) -> None:
        if self.resume_error is not None:
            raise self.resume_error
        self.resumes.append(worker_id)

    def events_for(
        self, *, worker_id: str | None = None, relative_path: str | None = None
    ) -> tuple[LogEvent, ...]:
        return tuple(
            event
            for event in self._snapshot.events
            if (relative_path is None or event.relative_path == relative_path)
            and (worker_id is None or event.worker_id == worker_id)
        )


class FakeControllerSession:
    """Stand-in for ``ControllerSession`` (window tests): client and lifecycle."""

    def __init__(
        self,
        client: FakeMonitorClient | None = None,
        *,
        hosted: bool = True,
        description: str = "hosting controller at http://127.0.0.1:8765",
    ) -> None:
        self.client = client if client is not None else FakeMonitorClient()
        self.hosted = hosted
        self.description = description
        self.closed = 0

    def close(self) -> None:
        self.closed += 1
