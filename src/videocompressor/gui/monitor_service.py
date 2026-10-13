"""Typed monitor data over controller contract v1 (issue #9).

Pure Python — no PySide6 import — so the payload parsers, the display
formatters, and the snapshot/event-tail session stay testable in CI without the
optional ``gui`` extra. Parsers are defensive: payloads are intentionally
minimal and additive, so missing, extra, or ill-typed fields degrade to
``None``/defaults instead of raising.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from videocompressor.gui.monitor_client import HttpControllerClient

__all__ = [
    "JobProgress",
    "LogEvent",
    "MonitorSession",
    "MonitorSnapshot",
    "SummaryCounts",
    "WorkerStatus",
    "format_eta",
    "format_last_seen",
    "format_progress",
    "format_roots",
    "format_speed",
    "format_summary",
    "parse_events",
    "parse_summary",
    "parse_workers",
]


@dataclass(frozen=True)
class JobProgress:
    """One heartbeat progress snapshot (``GET /workers`` ``progress``)."""

    phase: str | None = None
    pass_number: int | None = None
    percent: float | None = None
    fps: float | None = None
    speed: str | None = None
    eta_seconds: float | None = None


@dataclass(frozen=True)
class WorkerStatus:
    """One registered node as reported by ``GET /workers``."""

    worker_id: str
    name: str
    online: bool
    last_seen: datetime | None
    roots: dict[str, str]
    stop_requested: bool
    active_root_id: str | None
    active_path: str | None
    progress: JobProgress | None
    progress_at: datetime | None
    stop_mode: str | None = None
    machine_id: str | None = None


@dataclass(frozen=True)
class LogEvent:
    """One controller run-log event (``GET /events``)."""

    seq: int
    at: datetime | None
    level: str
    message: str
    worker_id: str | None = None
    root_id: str | None = None
    relative_path: str | None = None
    run_id: str | None = None


@dataclass(frozen=True)
class SummaryCounts:
    """Run summary counts (``GET /summary``)."""

    completed: int = 0
    skipped: int = 0
    failed: int = 0
    review: int = 0
    run_id: str | None = None


@dataclass(frozen=True)
class MonitorSnapshot:
    """One poll of the controller: nodes, the bounded event tail, and counts."""

    workers: tuple[WorkerStatus, ...]
    events: tuple[LogEvent, ...]
    summary: SummaryCounts
    fetched_at: datetime


class MonitorSession:
    """Poll the controller and keep a bounded run-log tail.

    The session is called from the monitor runner's worker thread only (one
    fetch in flight at a time); ``snapshot()`` advances the event cursor so each
    poll returns just the new events.
    """

    def __init__(self, client: HttpControllerClient, *, max_events: int = 1000) -> None:
        self._client = client
        self._events: deque[LogEvent] = deque(maxlen=max_events)
        self._cursor: int | None = None

    def snapshot(self) -> MonitorSnapshot:
        """Fetch nodes, the next event page, and the live summary counts."""
        workers = parse_workers(self._client.workers())
        events, next_seq = parse_events(self._client.events(after=self._cursor))
        self._events.extend(events)
        self._cursor = next_seq
        summary = parse_summary(self._client.summary())
        return MonitorSnapshot(
            workers=workers,
            events=tuple(self._events),
            summary=summary,
            fetched_at=datetime.now().astimezone(),
        )

    def stop_worker(self, worker_id: str, *, mode: str = "cancel") -> None:
        """Ask a worker to stop with ``mode`` (cancel now or drain)."""
        self._client.stop_worker(worker_id, mode=mode)

    def resume_worker(self, worker_id: str) -> None:
        """Clear a pending stop request."""
        self._client.resume_worker(worker_id)

    def events_for(
        self, *, worker_id: str | None = None, relative_path: str | None = None
    ) -> tuple[LogEvent, ...]:
        """The buffered events for one job (the per-job log tail)."""
        return tuple(
            event
            for event in self._events
            if (relative_path is None or event.relative_path == relative_path)
            and (worker_id is None or event.worker_id == worker_id)
        )


def parse_workers(payload: Mapping[str, Any]) -> tuple[WorkerStatus, ...]:
    """Parse a ``GET /workers`` body; malformed entries are skipped."""
    entries = payload.get("workers")
    if not isinstance(entries, list):
        return ()
    workers: list[WorkerStatus] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        worker_id = _optional_str(entry.get("worker_id"))
        if worker_id is None:
            continue
        active = _mapping(entry.get("active"))
        progress = _mapping(entry.get("progress"))
        workers.append(
            WorkerStatus(
                worker_id=worker_id,
                name=_optional_str(entry.get("name")) or worker_id,
                online=bool(entry.get("online")),
                last_seen=_optional_datetime(entry.get("last_seen")),
                roots=_str_map(entry.get("roots")),
                machine_id=_optional_str(entry.get("machine_id")),
                stop_requested=bool(entry.get("stop_requested")),
                stop_mode=_optional_str(entry.get("stop_mode")),
                active_root_id=_optional_str(active.get("root_id")),
                active_path=_optional_str(active.get("relative_path")),
                progress=_progress(progress) if progress else None,
                progress_at=_optional_datetime(entry.get("progress_at")),
            )
        )
    return tuple(workers)


def parse_events(payload: Mapping[str, Any]) -> tuple[tuple[LogEvent, ...], int]:
    """Parse a ``GET /events`` body into ``(events, next_sequence)``."""
    entries = payload.get("events")
    events: list[LogEvent] = []
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            seq = _optional_int(entry.get("seq"))
            message = _optional_str(entry.get("message"))
            if seq is None or message is None:
                continue
            events.append(
                LogEvent(
                    seq=seq,
                    at=_optional_datetime(entry.get("at")),
                    level=_optional_str(entry.get("level")) or "info",
                    message=message,
                    worker_id=_optional_str(entry.get("worker_id")),
                    root_id=_optional_str(entry.get("root_id")),
                    relative_path=_optional_str(entry.get("relative_path")),
                    run_id=_optional_str(entry.get("run_id")),
                )
            )
    next_seq = _optional_int(payload.get("next"))
    if next_seq is None:
        next_seq = events[-1].seq + 1 if events else 0
    return tuple(events), next_seq


def parse_summary(payload: Mapping[str, Any]) -> SummaryCounts:
    """Parse a ``GET /summary`` body."""
    return SummaryCounts(
        completed=_optional_int(payload.get("completed")) or 0,
        skipped=_optional_int(payload.get("skipped")) or 0,
        failed=_optional_int(payload.get("failed")) or 0,
        review=_optional_int(payload.get("review")) or 0,
        run_id=_optional_str(payload.get("run_id")),
    )


def format_eta(seconds: float | None) -> str:
    """Human-readable remaining time: ``-``, ``42s``, ``4m 12s``, ``1h 04m``."""
    if seconds is None:
        return "-"
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m {total % 60:02d}s"
    return f"{total // 3600}h {(total % 3600) // 60:02d}m"


def format_speed(progress: JobProgress | None) -> str:
    """``38.5 fps · 0.74x`` (parts omitted when absent), or ``-``."""
    if progress is None:
        return "-"
    parts: list[str] = []
    if progress.fps is not None:
        parts.append(f"{progress.fps:.1f} fps")
    if progress.speed:
        parts.append(progress.speed)
    return " · ".join(parts) or "-"


def format_progress(progress: JobProgress | None) -> str:
    """``encoding · pass 2 · 41.2%`` (parts omitted when absent), or ``-``."""
    if progress is None:
        return "-"
    parts: list[str] = []
    if progress.phase:
        parts.append(progress.phase)
    if progress.pass_number is not None:
        parts.append(f"pass {progress.pass_number}")
    if progress.percent is not None:
        parts.append(f"{progress.percent:.1f}%")
    return " · ".join(parts) or "-"


def format_last_seen(when: datetime | None, now: datetime) -> str:
    """``never`` / ``just now`` / ``12s ago`` / ``3m ago`` / ``2h 05m ago``."""
    if when is None:
        return "never"
    if when.tzinfo is None and now.tzinfo is not None:
        when = when.replace(tzinfo=now.tzinfo)
    elif now.tzinfo is None and when.tzinfo is not None:
        now = now.replace(tzinfo=when.tzinfo)
    delta = (now - when).total_seconds()
    if delta < 5:
        return "just now"
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    return f"{int(delta // 3600)}h {int(delta % 3600) // 60:02d}m ago"


def format_roots(roots: Mapping[str, str]) -> str:
    """``archive → /srv/archive; backup → /mnt/b``, or ``-`` when empty."""
    if not roots:
        return "-"
    return "; ".join(f"{root_id} → {path}" for root_id, path in roots.items())


def format_summary(counts: SummaryCounts) -> str:
    """``completed 3 · skipped 12 · failed 0 · review-needed 1``."""
    return (
        f"completed {counts.completed} · skipped {counts.skipped} ·"
        f" failed {counts.failed} · review-needed {counts.review}"
    )


def _progress(value: Mapping[str, Any]) -> JobProgress:
    return JobProgress(
        phase=_optional_str(value.get("phase")),
        pass_number=_optional_int(value.get("pass")),
        percent=_optional_float(value.get("percent")),
        fps=_optional_float(value.get("fps")),
        speed=_optional_str(value.get("speed")),
        eta_seconds=_optional_float(value.get("eta_seconds")),
    )


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _str_map(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {
        key: item
        for key, item in value.items()
        if isinstance(key, str) and isinstance(item, str)
    }


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _optional_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _optional_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
