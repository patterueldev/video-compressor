"""Creation-date stamping for the GUI (issue #86).

The Library's "Fix creation dates…" action and the automatic post-batch sweep
both run through :class:`DateStampRunner`, which executes one
:func:`videocompressor.core.date_stamp.sweep_folder` call off the UI thread
(the :mod:`videocompressor.gui.scan_runner` shape: injectable work, one run in
flight, results as signals).

The automatic sweep's decisions are pure helpers on plain values — which
destination may be swept, the ``since`` cutoff, the skip notes, and whether the
queue has drained. They are testable without Qt widgets or an event loop.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from videocompressor.core.config import AppConfig
from videocompressor.core.date_stamp import (
    StampReport,
    supported,
    sweep_folder,
    timezone_for_config,
)
from videocompressor.core.models import CaptureTimeMode, Destination, DestinationKind
from videocompressor.gui.monitor_service import MonitorSnapshot

__all__ = [
    "BATCH_SINCE_MARGIN",
    "BatchSweepDecision",
    "BatchSweepPlan",
    "BatchSweepWatcher",
    "DateStampRunner",
    "SweepRequest",
    "decide_batch_sweep",
    "describe_stamp_error",
    "manual_sweep_request",
    "queue_drained",
    "run_sweep",
    "settled_total",
    "sweep_request_for",
]

#: Margin subtracted from the enqueue time for the post-batch ``since`` filter.
BATCH_SINCE_MARGIN = timedelta(hours=1)


@dataclass(frozen=True)
class SweepRequest:
    """Everything one sweep needs, resolved before it leaves the GUI thread."""

    folder: Path
    mode: CaptureTimeMode
    timezone: str
    since: datetime | None = None
    dry_run: bool = False


def run_sweep(request: SweepRequest) -> StampReport:
    """Run one creation-date sweep (the runner's default work)."""
    return sweep_folder(
        request.folder,
        mode=request.mode,
        timezone=request.timezone,
        since=request.since,
        dry_run=request.dry_run,
    )


def manual_sweep_request(config: AppConfig, folder: Path) -> SweepRequest:
    """The Library action's request for ``folder`` under the app config."""
    return SweepRequest(
        folder=folder,
        mode=config.capture_time.mode,
        timezone=timezone_for_config(config),
    )


def describe_stamp_error(error: BaseException) -> str:
    """One-line message for the GUI's status area."""
    if isinstance(error, (ValueError, OSError)):
        return str(error)
    return f"unexpected error: {error!r}"


# -- automatic post-batch sweep --------------------------------------------


@dataclass(frozen=True)
class BatchSweepPlan:
    """An armed post-batch sweep for one destination root."""

    root_id: str
    folder: Path
    mode: CaptureTimeMode
    timezone: str
    since: datetime
    queued: int


@dataclass(frozen=True)
class BatchSweepDecision:
    """The armed plan, or the one-time skip note for the status area."""

    plan: BatchSweepPlan | None = None
    note: str | None = None


def decide_batch_sweep(
    config: AppConfig,
    destination: Destination | None,
    *,
    enqueued_at: datetime,
    queued: int,
) -> BatchSweepDecision:
    """Decide the post-batch sweep for one successful enqueue.

    Same-folder destinations are skipped silently: the outputs replaced the
    sources in their own folder, which the automatic path must never stamp.
    A root this machine cannot resolve is skipped with one status note, never
    an error dialog; anything else arms a plan sweeping the root's local path
    with ``since`` = enqueue time minus the margin.
    """
    if not supported() or destination is None or destination.kind is DestinationKind.SAME_FOLDER:
        return BatchSweepDecision()
    root_id = destination.root_id
    if root_id is None:  # defensive: an output-root destination always carries one
        return BatchSweepDecision()
    root = next((candidate for candidate in config.roots if candidate.id == root_id), None)
    if root is None:
        return BatchSweepDecision(
            note=f"creation dates: output root {root_id!r} is not registered on this machine"
        )
    folder = root.path.expanduser()
    if not folder.is_dir():
        return BatchSweepDecision(
            note=f"creation dates: output root {root.name!r} is not available on this machine"
        )
    return BatchSweepDecision(
        plan=BatchSweepPlan(
            root_id=root.id,
            folder=folder,
            mode=config.capture_time.mode,
            timezone=timezone_for_config(config),
            since=enqueued_at - BATCH_SINCE_MARGIN,
            queued=queued,
        )
    )


def sweep_request_for(plan: BatchSweepPlan) -> SweepRequest:
    """The runner request for an armed plan (the ``since`` filter included)."""
    return SweepRequest(
        folder=plan.folder,
        mode=plan.mode,
        timezone=plan.timezone,
        since=plan.since,
    )


def settled_total(snapshot: MonitorSnapshot) -> int:
    """The settled-clip total of one snapshot (completed/skipped/failed/review)."""
    counts = snapshot.summary
    return counts.completed + counts.skipped + counts.failed + counts.review


def queue_drained(snapshot: MonitorSnapshot, *, baseline_settled: int, queued: int) -> bool:
    """Whether one snapshot shows the batch's queue drained.

    No node may hold an active clip, and at least the batch's newly queued
    clips must have settled since the baseline; a clip waiting for a node keeps
    the sweep from running early.
    """
    if any(worker.active_path is not None for worker in snapshot.workers):
        return False
    return settled_total(snapshot) - baseline_settled >= queued


@dataclass
class BatchSweepWatcher:
    """Armed once per enqueue; yields its plan exactly once when drained."""

    plan: BatchSweepPlan
    baseline_settled: int
    triggered: bool = False

    def observe(self, snapshot: MonitorSnapshot) -> BatchSweepPlan | None:
        """Feed one monitor snapshot; the plan once the queue has drained."""
        if self.triggered:
            return None
        if not queue_drained(
            snapshot, baseline_settled=self.baseline_settled, queued=self.plan.queued
        ):
            return None
        self.triggered = True
        return self.plan


# -- background execution ---------------------------------------------------


class _DateStampTask(QRunnable):
    """One sweep on a pool thread; failures come back as a message."""

    def __init__(self, runner: DateStampRunner, request: SweepRequest) -> None:
        super().__init__()
        self._runner = runner
        self._request = request

    def run(self) -> None:
        try:
            report = self._runner.sweep(self._request)
        except Exception as error:  # surfaced to the user through the runner
            message = describe_stamp_error(error)
            try:
                self._runner.failed.emit(message)
            except RuntimeError:  # the window was destroyed mid-sweep
                pass
        else:
            try:
                self._runner.finished.emit(report)
            except RuntimeError:  # the window was destroyed mid-sweep
                pass


class DateStampRunner(QObject):
    """Run one creation-date sweep at a time and report it to the GUI thread."""

    started = Signal()
    finished = Signal(object)  # StampReport
    failed = Signal(str)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        pool: QThreadPool | None = None,
        sweep: Callable[[SweepRequest], StampReport] = run_sweep,
    ) -> None:
        super().__init__(parent)
        self._pool = pool if pool is not None else QThreadPool.globalInstance()
        self.sweep = sweep
        self._busy = False
        self.finished.connect(self._clear_busy)
        self.failed.connect(self._clear_busy)

    @property
    def busy(self) -> bool:
        """True while a sweep is in flight."""
        return self._busy

    def start(self, request: SweepRequest) -> bool:
        """Submit ``request``; returns ``False`` when a sweep is already running."""
        if self._busy:
            return False
        self._busy = True
        self.started.emit()
        self._pool.start(_DateStampTask(self, request))
        return True

    def _clear_busy(self, *_result: object) -> None:
        self._busy = False
