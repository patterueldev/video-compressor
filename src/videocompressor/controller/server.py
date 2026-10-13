"""Local HTTP controller facade: worker registry, durable queue, leases, run log.

Implements the v1 contract in ``docs/architecture.md`` plus the additive
endpoints documented there (``GET /workers``, ``GET /summary``, worker
``resume``). All HTTP handlers delegate to in-process methods on
:class:`ControllerServer`, so the GUI can host the controller and call it
directly while worker nodes use HTTP.

The facade composes the worker-registry, job-dispatch, enqueue, and lease-sweep
mixins; HTTP plumbing lives in ``controller/http.py`` and the error type plus
payload helpers live in ``controller/protocol.py``.

The durable queue is the SQLite manifest (``core/manifest.py``, issue #3);
tests inject :class:`MemoryManifest` when they want a lightweight double.
"""

from __future__ import annotations

import logging
import secrets
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from videocompressor.controller.constants import (
    EVENTS_PAGE_LIMIT,
    EVENTS_PAGE_MAX,
    LEASE_STATES,
    LEASE_TTL_SECONDS,
    SWEEP_INTERVAL_SECONDS,
    Clock,
    system_now,
)
from videocompressor.controller.enqueue import JobEnqueueMixin
from videocompressor.controller.http import _ControllerHTTPServer
from videocompressor.controller.jobs import JobDispatchMixin
from videocompressor.controller.memory_manifest import MemoryManifest
from videocompressor.controller.protocol import ControllerError, _event_dict, _run_dict
from videocompressor.controller.registry import WorkerRegistryMixin
from videocompressor.controller.store import ControllerStore, EventRow
from videocompressor.controller.sweep import LeaseSweepMixin
from videocompressor.core.config import DEFAULT_TEMPLATE_NAME
from videocompressor.core.fingerprint import fingerprint_file
from videocompressor.core.manifest import Manifest
from videocompressor.core.models import (
    ArchiveRoot,
    Classification,
    ClipRecord,
    Fingerprint,
    JobState,
    TemplateSettings,
)

__all__ = ["ControllerError", "ControllerServer", "new_token", "open_manifest"]

_log = logging.getLogger("videocompressor.controller")


def new_token() -> str:
    """Generate a fresh bearer token for the controller API."""
    return secrets.token_urlsafe(32)


def _no_roots() -> tuple[ArchiveRoot, ...]:
    """Default roots provider: no registered roots, so enqueue rejects every file."""
    return ()


def open_manifest(
    db_path: Path,
    *,
    clock: Clock = system_now,
    lease_ttl_seconds: int = LEASE_TTL_SECONDS,
) -> tuple[Manifest, bool]:
    """Open the queue manifest, falling back to the in-memory double.

    The SQLite manifest (issue #3) is preferred; the in-memory double keeps
    ``vc controller`` usable (with a warning) if the manifest is unavailable.
    ``lease_ttl_seconds`` becomes the manifest's job-lease TTL (issue #75).
    Returns ``(manifest, durable)``.
    """
    try:
        return Manifest.open(db_path, lease_ttl_seconds=lease_ttl_seconds), True
    except NotImplementedError:
        return MemoryManifest.open(db_path, clock=clock, lease_ttl_seconds=lease_ttl_seconds), False


class ControllerServer(WorkerRegistryMixin, JobDispatchMixin, JobEnqueueMixin, LeaseSweepMixin):
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
        roots: Callable[[], Sequence[ArchiveRoot]] | None = None,
        fingerprint: Callable[[Path], Fingerprint] = fingerprint_file,
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
        self._roots = roots if roots is not None else _no_roots
        self._fingerprint = fingerprint
        self.lease_ttl_seconds = lease_ttl_seconds
        self.sweep_interval_seconds = sweep_interval_seconds
        self._clock = clock
        self._host = host
        self._port = port
        self._lock = threading.RLock()
        self._progress: dict[str, dict[str, Any]] = {}
        self._online: set[str] = set()
        self._httpd: _ControllerHTTPServer | None = None
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

    # -- internals ---------------------------------------------------------

    def _find_clip(self, root_id: str, relative_path: str) -> ClipRecord | None:
        return self.manifest.get_clip(root_id, relative_path)

    def _find_active_clip(self, worker_id: str) -> ClipRecord | None:
        for record in self.manifest.list_clips():
            if record.worker_id == worker_id and record.state in LEASE_STATES:
                return record
        return None

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
