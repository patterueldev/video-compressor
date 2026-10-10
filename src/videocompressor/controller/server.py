"""Local HTTP controller: worker registry, durable queue, leases, and run log.

Implements the v1 contract in ``docs/architecture.md`` plus the additive
endpoints documented there (``GET /workers``, ``GET /summary``, worker
``resume``). All HTTP handlers delegate to in-process methods on
:class:`ControllerServer`, so the GUI can host the controller and call it
directly while worker nodes use HTTP.

The durable queue is the manifest (``core/manifest.py``); while issue #3 is
unimplemented the server can be constructed against :class:`MemoryManifest`.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
import secrets
import threading
from collections.abc import Mapping
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from videocompressor.controller.constants import (
    ACTIVE_STATES,
    EVENTS_PAGE_LIMIT,
    EVENTS_PAGE_MAX,
    HEARTBEAT_INTERVAL_SECONDS,
    LEASE_TTL_SECONDS,
    MAX_BODY_BYTES,
    SWEEP_INTERVAL_SECONDS,
    WORKER_ID_PATTERN,
    Clock,
    system_now,
)
from videocompressor.controller.memory_manifest import MemoryManifest
from videocompressor.controller.store import ControllerStore, EventRow, RunRow, WorkerRow
from videocompressor.core.config import DEFAULT_TEMPLATE_NAME
from videocompressor.core.manifest import Manifest
from videocompressor.core.models import (
    Classification,
    ClipRecord,
    Fingerprint,
    JobState,
    TemplateSettings,
)

__all__ = ["ControllerError", "ControllerServer", "new_token", "open_manifest"]

_log = logging.getLogger("videocompressor.controller")

#: Progress phases a worker reports that also advance the manifest stage.
_PHASE_STATES = {
    "copying": JobState.COPIED,
    "copied": JobState.COPIED,
    "encoding": JobState.ENCODING,
    "validating": JobState.VALIDATED,
    "validated": JobState.VALIDATED,
    "transferring": JobState.TRANSFERRING,
    "replacing": JobState.REPLACING,
}
_STAGE_ORDER = (
    JobState.CLAIMED,
    JobState.COPIED,
    JobState.ENCODING,
    JobState.VALIDATED,
    JobState.TRANSFERRING,
    JobState.REPLACING,
)


class ControllerError(Exception):
    """An HTTP-mappable controller error (status, slug, and human message)."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def new_token() -> str:
    """Generate a fresh bearer token for the controller API."""
    return secrets.token_urlsafe(32)


def open_manifest(
    db_path: Path,
    *,
    clock: Clock = system_now,
    lease_ttl_seconds: int = LEASE_TTL_SECONDS,
) -> tuple[Manifest, bool]:
    """Open the queue manifest, falling back to the in-memory stub.

    Returns ``(manifest, durable)``; ``durable`` is False while issue #3's
    SQLite manifest is still unimplemented and the stub is in use.
    """
    try:
        return Manifest.open(db_path), True
    except NotImplementedError:
        return MemoryManifest.open(db_path, clock=clock, lease_ttl_seconds=lease_ttl_seconds), False


def _unknown_worker(worker_id: str) -> ControllerError:
    return ControllerError(404, "unknown_worker", f"worker {worker_id!r} is not registered")


class ControllerServer:
    """Serve the controller contract v1 and expose the same operations in-process."""

    def __init__(
        self,
        *,
        store: ControllerStore,
        manifest: Manifest,
        token: str,
        host: str = "127.0.0.1",
        port: int = 8765,
        templates: Mapping[str, TemplateSettings] | None = None,
        clock: Clock = system_now,
        lease_ttl_seconds: int = LEASE_TTL_SECONDS,
        sweep_interval_seconds: float = SWEEP_INTERVAL_SECONDS,
    ) -> None:
        if not token:
            raise ValueError("the controller token must not be empty")
        self.store = store
        self.manifest = manifest
        self.token = token
        if templates is not None:
            self.templates = dict(templates)
        else:
            self.templates = {DEFAULT_TEMPLATE_NAME: TemplateSettings()}
        self.lease_ttl_seconds = lease_ttl_seconds
        self.sweep_interval_seconds = sweep_interval_seconds
        self._clock = clock
        self._host = host
        self._port = port
        self._lock = threading.RLock()
        self._progress: dict[str, dict[str, Any]] = {}
        self._online: set[str] = set()
        self._httpd: ThreadingHTTPServer | None = None
        self._http_thread: threading.Thread | None = None
        self._sweep_thread: threading.Thread | None = None
        self._stopping = threading.Event()
        active = store.active_run()
        self._active_run_id: str | None = active.run_id if active is not None else None

    # -- lifecycle ---------------------------------------------------------

    @property
    def port(self) -> int:
        """Actual bound port once started (useful when constructed with port 0)."""
        if self._httpd is not None:
            return int(self._httpd.server_address[1])
        return self._port

    @property
    def base_url(self) -> str:
        """Base URL of the controller (without the ``/api/v1`` prefix)."""
        host = self._host
        if ":" in host:
            host = f"[{host}]"
        return f"http://{host}:{self.port}"

    def start_background(self) -> None:
        """Bind and start serving plus the lease sweeper in background threads."""
        if self._httpd is not None:
            raise RuntimeError("controller is already running")
        httpd = _ControllerHTTPServer((self._host, self._port), self)
        self._httpd = httpd
        self._http_thread = threading.Thread(
            target=httpd.serve_forever,
            kwargs={"poll_interval": 0.1},
            name="vc-controller-http",
            daemon=True,
        )
        self._http_thread.start()
        self._sweep_thread = threading.Thread(
            target=self._sweep_loop, name="vc-controller-sweep", daemon=True
        )
        self._sweep_thread.start()
        _log.info("controller listening on %s", self.base_url)

    def wait(self) -> None:
        """Block until the server stops (Ctrl-C in the CLI interrupts this)."""
        thread = self._http_thread
        if thread is None:
            raise RuntimeError("controller is not running")
        thread.join()

    def stop(self) -> None:
        """Stop serving and sweeping, then close the store; safe to call twice."""
        self._stopping.set()
        httpd, self._httpd = self._httpd, None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        for attribute in ("_http_thread", "_sweep_thread"):
            thread = getattr(self, attribute)
            if thread is not None:
                thread.join(timeout=5)
                setattr(self, attribute, None)
        self.store.close()

    def __enter__(self) -> ControllerServer:
        self.start_background()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    # -- worker registry ---------------------------------------------------

    def register_worker(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Register or refresh a worker; returns the lease/heartbeat intervals."""
        with self._lock:
            worker_id = _require_str(payload, "worker_id")
            if not WORKER_ID_PATTERN.fullmatch(worker_id):
                raise ControllerError(
                    400, "invalid_worker_id", "worker_id must match [A-Za-z0-9_.-]{1,64}"
                )
            name = _optional_str(payload, "name", default=worker_id) or worker_id
            roots = _optional_str_map(payload, "roots")
            scratch_dir = _optional_str(payload, "scratch_dir")
            thread_cap = _optional_int(payload, "thread_cap", default=2)
            version = _optional_str(payload, "version")
            self.store.upsert_worker(
                worker_id,
                name=name,
                roots=roots,
                scratch_dir=scratch_dir,
                thread_cap=thread_cap,
                version=version,
            )
            self._mark_online(worker_id)
            self._event("info", f"worker {worker_id} registered", worker_id=worker_id)
            return {
                "lease_seconds": self.lease_ttl_seconds,
                "heartbeat_seconds": HEARTBEAT_INTERVAL_SECONDS,
            }

    def heartbeat(self, worker_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Renew the worker lease and the active job lease; returns the stop flag."""
        with self._lock:
            row = self.store.touch_worker(worker_id)
            if row is None:
                raise _unknown_worker(worker_id)
            self._mark_online(worker_id)
            active = payload.get("active")
            progress = payload.get("progress")
            if active is not None:
                if not isinstance(active, Mapping):
                    raise ControllerError(400, "bad_request", "'active' must be an object")
                root_id = _require_str(active, "root_id")
                relative_path = _require_str(active, "relative_path")
                record = self._find_clip(root_id, relative_path)
                if (
                    record is None
                    or record.worker_id != worker_id
                    or record.state not in ACTIVE_STATES
                ):
                    raise ControllerError(
                        409,
                        "lease_conflict",
                        f"worker {worker_id!r} does not hold {root_id}:{relative_path}",
                    )
                self.manifest.heartbeat(root_id, relative_path, worker_id)
                self._progress[worker_id] = {
                    "active": {"root_id": root_id, "relative_path": relative_path},
                    "progress": dict(progress) if isinstance(progress, Mapping) else None,
                    "updated_at": self._clock().isoformat(),
                }
                self._apply_phase(record, progress)
            else:
                self._progress.pop(worker_id, None)
                if row.stop_requested:
                    self._ack_stop_without_report(worker_id)
            current = self.store.get_worker(worker_id)
            stop = bool(current.stop_requested) if current is not None else False
            return {"stop": stop, "lease_seconds": self.lease_ttl_seconds}

    def workers(self) -> dict[str, Any]:
        """Registry snapshot with online state, active clip, and latest progress."""
        with self._lock:
            now = self._clock()
            entries = []
            for row in self.store.list_workers():
                active = self._find_active_clip(row.worker_id)
                cached = self._progress.get(row.worker_id, {})
                entries.append(
                    {
                        "worker_id": row.worker_id,
                        "name": row.name,
                        "online": self._is_online(row, now),
                        "last_seen": row.last_seen.isoformat() if row.last_seen else None,
                        "roots": row.roots,
                        "scratch_dir": row.scratch_dir,
                        "thread_cap": row.thread_cap,
                        "version": row.version,
                        "stop_requested": row.stop_requested,
                        "active": (
                            {"root_id": active.root_id, "relative_path": active.relative_path}
                            if active is not None
                            else None
                        ),
                        "progress": cached.get("progress"),
                        "progress_at": cached.get("updated_at"),
                    }
                )
            return {"workers": entries}

    def stop_worker(self, worker_id: str) -> None:
        """Ask a worker to terminate its active job at the next heartbeat."""
        with self._lock:
            row = self.store.get_worker(worker_id)
            if row is None:
                raise _unknown_worker(worker_id)
            active = self._find_active_clip(worker_id)
            if active is None:
                self._event(
                    "info", f"stop requested for {worker_id}: no active job", worker_id=worker_id
                )
                return
            if not row.stop_requested:
                self.store.set_stop_requested(worker_id, True)
                self._event(
                    "info",
                    f"stop requested for {worker_id} (active: {active.relative_path})",
                    worker_id=worker_id,
                    root_id=active.root_id,
                    relative_path=active.relative_path,
                )

    def resume_worker(self, worker_id: str) -> None:
        """Clear a pending stop request."""
        with self._lock:
            row = self.store.get_worker(worker_id)
            if row is None:
                raise _unknown_worker(worker_id)
            if row.stop_requested:
                self.store.set_stop_requested(worker_id, False)
                self._event("info", f"resumed {worker_id}", worker_id=worker_id)

    # -- jobs --------------------------------------------------------------

    def claim(self, worker_id: str) -> dict[str, Any] | None:
        """Claim the next queued clip; None when nothing is available."""
        with self._lock:
            if self.store.get_worker(worker_id) is None:
                raise _unknown_worker(worker_id)
            self.store.touch_worker(worker_id)
            self._mark_online(worker_id)
            record = self.manifest.claim_next(worker_id)
            if record is None:
                return None
            template_id, template = self._template_for(record)
            settings = self._effective_settings(template, record)
            settings_hash = _settings_hash(settings)
            if record.template_id != template_id or record.settings_hash != settings_hash:
                self.manifest.update_state(
                    record.root_id,
                    record.relative_path,
                    JobState.CLAIMED,
                    template_id=template_id,
                    settings_hash=settings_hash,
                )
            capture_time = record.capture_time
            self._progress.pop(worker_id, None)
            self._event(
                "info",
                f"claimed {record.relative_path}",
                worker_id=worker_id,
                root_id=record.root_id,
                relative_path=record.relative_path,
            )
            return {
                "job": {
                    "root_id": record.root_id,
                    "relative_path": record.relative_path,
                    "template_id": template_id,
                    "settings": settings,
                    "settings_hash": settings_hash,
                    "capture_time": (
                        capture_time.when.isoformat()
                        if capture_time is not None and capture_time.when is not None
                        else None
                    ),
                }
            }

    def complete(self, payload: Mapping[str, Any]) -> None:
        """Mark the held clip complete with its output fingerprint."""
        with self._lock:
            worker_id, root_id, relative_path, _ = self._held_clip(payload)
            output = payload.get("output")
            if not isinstance(output, Mapping):
                raise ControllerError(400, "bad_request", "'output' must be an object")
            size_bytes = _require_int(output, "size_bytes")
            digest = _require_str(output, "digest")
            mtime_ns = _optional_int(output, "mtime_ns", default=0)
            self.manifest.update_state(
                root_id,
                relative_path,
                JobState.COMPLETE,
                output=Fingerprint(size_bytes=size_bytes, mtime_ns=mtime_ns, digest=digest),
                completed_at=self._clock(),
                error=None,
            )
            self._finish_job(worker_id)
            self._event(
                "info",
                f"completed {relative_path}",
                worker_id=worker_id,
                root_id=root_id,
                relative_path=relative_path,
            )

    def fail(self, payload: Mapping[str, Any]) -> None:
        """Report a terminal failure or hand the clip back to the queue."""
        with self._lock:
            worker_id, root_id, relative_path, _ = self._held_clip(payload)
            error = _optional_str(payload, "error", default=None)
            disposition = _optional_str(payload, "disposition", default="failed")
            if disposition not in {"review", "failed", "requeue"}:
                raise ControllerError(
                    400, "bad_request", "'disposition' must be review, failed, or requeue"
                )
            row = self.store.get_worker(worker_id)
            stopped = bool(row is not None and row.stop_requested)
            if disposition == "requeue":
                self.manifest.update_state(
                    root_id, relative_path, JobState.QUEUED, worker_id=None, error=None
                )
                reason = "stopped by operator" if stopped else (error or "worker request")
                message = f"requeued {relative_path} ({reason})"
                level = "info"
            elif disposition == "review":
                self.manifest.update_state(
                    root_id, relative_path, JobState.REVIEW, error=error or "needs review"
                )
                message = f"{relative_path} needs review: {error or 'unspecified'}"
                level = "warning"
            else:
                self.manifest.update_state(
                    root_id, relative_path, JobState.FAILED, error=error or "failed"
                )
                message = f"failed {relative_path}: {error or 'unspecified'}"
                level = "error"
            self._progress.pop(worker_id, None)
            self._clear_stop_after_job(worker_id, row)
            self._event(
                level,
                message,
                worker_id=worker_id,
                root_id=root_id,
                relative_path=relative_path,
            )

    # -- run log and summaries --------------------------------------------

    def events(self, *, after: int | None = None, limit: int = EVENTS_PAGE_LIMIT) -> dict[str, Any]:
        """Bounded run-log page with the next sequence to fetch from."""
        with self._lock:
            bounded = max(1, min(limit, EVENTS_PAGE_MAX))
            rows = self.store.list_events(after=after, limit=bounded)
            next_seq = rows[-1].seq + 1 if rows else (after or 0)
            return {"events": [_event_dict(row) for row in rows], "next": next_seq}

    def summary(self, *, run_id: str | None = None) -> dict[str, Any]:
        """Run summary counts, from a finished run or computed live."""
        with self._lock:
            if run_id is not None:
                run = self.store.get_run(run_id)
                if run is None:
                    raise ControllerError(404, "unknown_run", f"run {run_id!r} is unknown")
                if run.ended_at is not None:
                    counts: dict[str, int] = {
                        "completed": run.completed or 0,
                        "skipped": run.skipped or 0,
                        "failed": run.failed or 0,
                        "review": run.review or 0,
                    }
                else:
                    counts = self._live_counts()
            else:
                counts = self._live_counts()
            return {**counts, "run_id": run_id, "generated_at": self._clock().isoformat()}

    def start_run(self) -> dict[str, Any]:
        """Open a processing run; events are tagged with its id until it finishes."""
        with self._lock:
            if self._active_run_id is not None or self.store.active_run() is not None:
                raise ControllerError(409, "run_active", "a run is already active")
            run = self.store.start_run()
            self._active_run_id = run.run_id
            self._event("info", f"run {run.run_id} started", run_id=run.run_id)
            return _run_dict(run)

    def finish_run(self) -> dict[str, Any]:
        """Close the active run and persist its summary counts."""
        with self._lock:
            run = self.store.active_run()
            if run is None:
                raise ControllerError(409, "run_not_active", "no run is active")
            counts = self._live_counts()
            finished = self.store.finish_run(run.run_id, **counts)
            self._active_run_id = None
            self._event(
                "info",
                f"run {finished.run_id} finished: {counts['completed']} completed,"
                f" {counts['skipped']} skipped, {counts['failed']} failed,"
                f" {counts['review']} review",
                run_id=finished.run_id,
            )
            return _run_dict(finished)

    # -- lease sweep -------------------------------------------------------

    def sweep_once(self) -> None:
        """One housekeeping pass: reconcile expired leases and mark offline workers."""
        with self._lock:
            recovered = self.manifest.reconcile()
            for record in recovered:
                if record.state is JobState.REVIEW:
                    self._event(
                        "warning",
                        f"{record.relative_path} parked in review:"
                        " lease expired during replacement, recovery required",
                        worker_id=record.worker_id,
                        root_id=record.root_id,
                        relative_path=record.relative_path,
                    )
                elif record.state is JobState.QUEUED:
                    self._event(
                        "warning",
                        f"requeued {record.relative_path}: lease expired",
                        root_id=record.root_id,
                        relative_path=record.relative_path,
                    )
                else:  # pragma: no cover - defensive for future manifest semantics
                    self._event(
                        "info",
                        f"reconciled {record.relative_path} to {record.state}",
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
                if row.stop_requested and self._find_active_clip(row.worker_id) is None:
                    self.store.set_stop_requested(row.worker_id, False)

    def _sweep_loop(self) -> None:
        while not self._stopping.wait(self.sweep_interval_seconds):
            try:
                self.sweep_once()
            except Exception:  # keep the controller alive through one bad sweep
                _log.exception("controller sweep failed")

    # -- internals ---------------------------------------------------------

    def _find_clip(self, root_id: str, relative_path: str) -> ClipRecord | None:
        for record in self.manifest.list_clips():
            if record.root_id == root_id and record.relative_path == relative_path:
                return record
        return None

    def _find_active_clip(self, worker_id: str) -> ClipRecord | None:
        for record in self.manifest.list_clips():
            if record.worker_id == worker_id and record.state in ACTIVE_STATES:
                return record
        return None

    def _held_clip(self, payload: Mapping[str, Any]) -> tuple[str, str, str, ClipRecord]:
        worker_id = _require_str(payload, "worker_id")
        root_id = _require_str(payload, "root_id")
        relative_path = _require_str(payload, "relative_path")
        record = self._find_clip(root_id, relative_path)
        if record is None:
            raise ControllerError(404, "unknown_job", f"no job for {root_id}:{relative_path}")
        if record.worker_id != worker_id or record.state not in ACTIVE_STATES:
            raise ControllerError(
                409,
                "lease_conflict",
                f"worker {worker_id!r} does not hold {root_id}:{relative_path}",
            )
        return worker_id, root_id, relative_path, record

    def _template_for(self, record: ClipRecord) -> tuple[str, TemplateSettings]:
        template_id = record.template_id or DEFAULT_TEMPLATE_NAME
        return template_id, self.templates.get(template_id, TemplateSettings())

    def _effective_settings(
        self, template: TemplateSettings, record: ClipRecord
    ) -> dict[str, Any]:
        """Resolve the day/evening bucket per clip (contract note on ``claim``)."""
        when = record.capture_time.when if record.capture_time is not None else None
        evening = False
        if when is not None:
            if when.tzinfo is not None:
                with contextlib.suppress(ZoneInfoNotFoundError):
                    when = when.astimezone(ZoneInfo(template.timezone))
            evening = when.time() >= template.evening_start
        return {
            "output_height": template.output_height,
            "video_bitrate_kbps": (
                template.evening_video_bitrate_kbps
                if evening
                else template.daytime_video_bitrate_kbps
            ),
            "audio_bitrate_kbps": template.audio_bitrate_kbps,
            "ffmpeg_preset": template.ffmpeg_preset,
        }

    def _apply_phase(self, record: ClipRecord, progress: object) -> None:
        if not isinstance(progress, Mapping):
            return
        phase = progress.get("phase")
        if not isinstance(phase, str):
            return
        target = _PHASE_STATES.get(phase.lower())
        if target is None or target not in ACTIVE_STATES or target is record.state:
            return
        if _STAGE_ORDER.index(target) <= _STAGE_ORDER.index(record.state):
            return
        self.manifest.update_state(record.root_id, record.relative_path, target)

    def _live_counts(self) -> dict[str, int]:
        counts = {"completed": 0, "skipped": 0, "failed": 0, "review": 0}
        for record in self.manifest.list_clips():
            if record.state is JobState.COMPLETE:
                counts["completed"] += 1
            elif record.state is JobState.FAILED:
                counts["failed"] += 1
            elif record.state is JobState.REVIEW:
                counts["review"] += 1
            elif record.classification is Classification.COMPLETE:
                counts["skipped"] += 1
        return counts

    def _finish_job(self, worker_id: str) -> None:
        self._progress.pop(worker_id, None)
        row = self.store.get_worker(worker_id)
        self._clear_stop_after_job(worker_id, row)

    def _clear_stop_after_job(self, worker_id: str, row: WorkerRow | None) -> None:
        if row is not None and row.stop_requested:
            self.store.set_stop_requested(worker_id, False)
            self._event("info", f"stop cleared for {worker_id}", worker_id=worker_id)

    def _ack_stop_without_report(self, worker_id: str) -> None:
        active = self._find_active_clip(worker_id)
        if active is not None:
            self.manifest.update_state(
                active.root_id, active.relative_path, JobState.QUEUED, worker_id=None, error=None
            )
            self._event(
                "warning",
                f"requeued {active.relative_path}: {worker_id} stopped without reporting",
                worker_id=worker_id,
                root_id=active.root_id,
                relative_path=active.relative_path,
            )
        self.store.set_stop_requested(worker_id, False)
        self._event("info", f"stop cleared for {worker_id}", worker_id=worker_id)

    def _mark_online(self, worker_id: str) -> None:
        if worker_id not in self._online:
            self._online.add(worker_id)
            self._event("info", f"worker {worker_id} online", worker_id=worker_id)

    def _is_online(self, row: WorkerRow, now: datetime) -> bool:
        if row.last_seen is None:
            return False
        return (now - row.last_seen).total_seconds() <= self.lease_ttl_seconds

    def _event(
        self,
        level: str,
        message: str,
        *,
        worker_id: str | None = None,
        root_id: str | None = None,
        relative_path: str | None = None,
        run_id: str | None = None,
    ) -> EventRow:
        return self.store.append_event(
            level,
            message,
            worker_id=worker_id,
            root_id=root_id,
            relative_path=relative_path,
            run_id=run_id if run_id is not None else self._active_run_id,
        )


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

_WORKER_ID_SEGMENT = object()

_ROUTES: tuple[tuple[str, tuple[object, ...], str], ...] = (
    ("POST", ("workers", "register"), "register"),
    ("POST", ("workers", _WORKER_ID_SEGMENT, "heartbeat"), "heartbeat"),
    ("POST", ("workers", _WORKER_ID_SEGMENT, "stop"), "stop"),
    ("POST", ("workers", _WORKER_ID_SEGMENT, "resume"), "resume"),
    ("POST", ("jobs", "claim"), "claim"),
    ("POST", ("jobs", "complete"), "complete"),
    ("POST", ("jobs", "fail"), "fail"),
    ("GET", ("events",), "events"),
    ("GET", ("workers",), "workers"),
    ("GET", ("summary",), "summary"),
)


class _ControllerHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], controller: ControllerServer) -> None:
        super().__init__(address, _RequestHandler)
        self.controller = controller


class _RequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _ControllerHTTPServer

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._dispatch("POST")

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - API name
        _log.debug("%s - %s", self.address_string(), format % args)

    # -- plumbing ----------------------------------------------------------

    def _dispatch(self, method: str) -> None:
        try:
            self._require_auth()
            parts = [unquote(part) for part in urlsplit(self.path).path.split("/") if part]
            rest = parts[2:] if parts[:2] == ["api", "v1"] else []
            route = _match_route(method, rest)
            if route is None:
                if _route_shape_matches(rest):
                    raise ControllerError(
                        405, "method_not_allowed", f"{method} is not allowed here"
                    )
                raise ControllerError(404, "not_found", f"no route for {method} {self.path}")
            name, captured = route
            payload = self._read_json() if method == "POST" else {}
            status, body = self._handle(name, captured, payload)
            self._send_json(status, body)
        except ControllerError as exc:
            with contextlib.suppress(Exception):
                self._send_json(exc.status, {"error": {"code": exc.code, "message": exc.message}})
        except Exception:  # pragma: no cover - defensive
            _log.exception("unhandled controller error")
            with contextlib.suppress(Exception):
                self._send_json(
                    500, {"error": {"code": "internal", "message": "internal controller error"}}
                )

    def _handle(
        self, name: str, captured: dict[str, str], payload: Mapping[str, Any]
    ) -> tuple[int, dict[str, Any] | None]:
        controller = self.server.controller
        if name == "register":
            return 200, controller.register_worker(payload)
        if name == "heartbeat":
            return 200, controller.heartbeat(captured["worker_id"], payload)
        if name == "stop":
            controller.stop_worker(captured["worker_id"])
            return 204, None
        if name == "resume":
            controller.resume_worker(captured["worker_id"])
            return 204, None
        if name == "claim":
            job = controller.claim(_require_str(payload, "worker_id"))
            return (200, job) if job is not None else (204, None)
        if name == "complete":
            controller.complete(payload)
            return 204, None
        if name == "fail":
            controller.fail(payload)
            return 204, None
        if name == "events":
            params = parse_qs(urlsplit(self.path).query)
            after = _query_int(params, "after")
            limit = _query_int(params, "limit")
            return 200, controller.events(
                after=after, limit=limit if limit is not None else EVENTS_PAGE_LIMIT
            )
        if name == "workers":
            return 200, controller.workers()
        if name == "summary":
            params = parse_qs(urlsplit(self.path).query)
            run_id = params.get("run_id", [None])[0]
            return 200, controller.summary(run_id=run_id)
        raise ControllerError(500, "internal", f"unhandled route {name}")  # pragma: no cover

    def _require_auth(self) -> None:
        header = self.headers.get("Authorization", "")
        prefix = "Bearer "
        token = header[len(prefix) :] if header.startswith(prefix) else ""
        if not token or not hmac.compare_digest(token, self.server.controller.token):
            raise ControllerError(401, "unauthorized", "missing or invalid bearer token")

    def _read_json(self) -> Mapping[str, Any]:
        length_header = self.headers.get("Content-Length")
        if length_header is None:
            return {}
        try:
            length = int(length_header)
        except ValueError:
            raise ControllerError(400, "bad_request", "invalid Content-Length") from None
        if length < 0 or length > MAX_BODY_BYTES:
            raise ControllerError(400, "bad_request", "request body is too large")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            raise ControllerError(400, "bad_request", "body is not valid JSON") from None
        if not isinstance(data, dict):
            raise ControllerError(400, "bad_request", "body must be a JSON object")
        return data

    def _send_json(self, status: int, payload: dict[str, Any] | None) -> None:
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        if body:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if body:
            self.wfile.write(body)
        self.close_connection = True


def _match_route(method: str, rest: list[str]) -> tuple[str, dict[str, str]] | None:
    for route_method, segments, name in _ROUTES:
        if route_method != method or len(segments) != len(rest):
            continue
        captured: dict[str, str] = {}
        for segment, value in zip(segments, rest, strict=True):
            if segment is _WORKER_ID_SEGMENT:
                captured["worker_id"] = value
            elif segment != value:
                break
        else:
            return name, captured
    return None


def _route_shape_matches(rest: list[str]) -> bool:
    return any(
        len(segments) == len(rest)
        and all(
            segment is _WORKER_ID_SEGMENT or segment == value
            for segment, value in zip(segments, rest, strict=True)
        )
        for _, segments, _ in _ROUTES
    )


def _query_int(params: Mapping[str, list[str]], name: str) -> int | None:
    values = params.get(name)
    if not values:
        return None
    try:
        return int(values[0])
    except ValueError:
        raise ControllerError(400, "bad_request", f"{name!r} must be an integer") from None


# ---------------------------------------------------------------------------
# payload helpers
# ---------------------------------------------------------------------------


def _require_str(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ControllerError(400, "bad_request", f"{key!r} must be a non-empty string")
    return value


def _optional_str(
    payload: Mapping[str, Any], key: str, *, default: str | None = None
) -> str | None:
    value = payload.get(key, default)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ControllerError(400, "bad_request", f"{key!r} must be a string")
    return value


def _require_int(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ControllerError(400, "bad_request", f"{key!r} must be an integer")
    return value


def _optional_int(payload: Mapping[str, Any], key: str, *, default: int) -> int:
    value = payload.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ControllerError(400, "bad_request", f"{key!r} must be an integer")
    return value


def _optional_str_map(payload: Mapping[str, Any], key: str) -> dict[str, str]:
    value = payload.get(key)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ControllerError(400, "bad_request", f"{key!r} must be an object")
    out: dict[str, str] = {}
    for map_key, map_value in value.items():
        if not isinstance(map_key, str) or not isinstance(map_value, str):
            raise ControllerError(400, "bad_request", f"{key!r} must map strings to strings")
        out[map_key] = map_value
    return out


def _settings_hash(settings: Mapping[str, Any]) -> str:
    canonical = json.dumps(settings, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _event_dict(event: EventRow) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "seq": event.seq,
        "at": event.at.isoformat(),
        "level": event.level,
        "message": event.message,
    }
    if event.worker_id is not None:
        payload["worker_id"] = event.worker_id
    if event.root_id is not None:
        payload["root_id"] = event.root_id
    if event.relative_path is not None:
        payload["relative_path"] = event.relative_path
    if event.run_id is not None:
        payload["run_id"] = event.run_id
    return payload


def _run_dict(run: RunRow) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "started_at": run.started_at.isoformat(),
        "ended_at": run.ended_at.isoformat() if run.ended_at else None,
        "completed": run.completed,
        "skipped": run.skipped,
        "failed": run.failed,
        "review": run.review,
    }
