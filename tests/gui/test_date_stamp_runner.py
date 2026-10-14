"""Creation-date runner and post-batch sweep decisions (issue #86).

The decision tests are pure (no widgets, no event loop); the runner tests use a
real ``QThreadPool`` like the scan-runner suite; the window tests drive the
MonitorWindow wiring with stub panels and a fake runner.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, QThreadPool, Signal
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QWidget

from gui.monitor_support import (
    FakeControllerSession,
    make_idle_worker,
    make_snapshot,
    make_summary,
)
from gui.monitor_support_qt import FakeMonitorRunner, FakeScanRunner, dispose, make_library
from videocompressor.core.config import AppConfig, register_root
from videocompressor.core.date_stamp import StampReport, StampResult, StampStatus
from videocompressor.core.models import CaptureTimeMode, Destination, DestinationKind
from videocompressor.gui import date_stamp_runner, monitor_window
from videocompressor.gui.date_stamp_runner import (
    BATCH_SINCE_MARGIN,
    BatchSweepDecision,
    BatchSweepPlan,
    BatchSweepWatcher,
    DateStampRunner,
    SweepRequest,
    decide_batch_sweep,
    queue_drained,
    settled_total,
)

MODE = CaptureTimeMode.LOCAL_WALL_CLOCK
ENQUEUED_AT = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)


def _config(tmp_path: Path) -> tuple[AppConfig, Path]:
    config = AppConfig()
    folder = tmp_path / "compressed"
    folder.mkdir()
    register_root(config, root_id="compressed", name="Compressed", path=folder)
    return config, folder


def _output_root_destination() -> Destination:
    return Destination(DestinationKind.OUTPUT_ROOT, "compressed", "")


# -- decisions --------------------------------------------------------------


def test_output_root_destination_arms_a_plan_with_the_enqueue_time_margin(tmp_path: Path):
    config, folder = _config(tmp_path)

    decision = decide_batch_sweep(
        config, _output_root_destination(), enqueued_at=ENQUEUED_AT, queued=3
    )

    assert decision.note is None
    plan = decision.plan
    assert plan is not None
    assert plan.root_id == "compressed"
    assert plan.folder == folder
    assert plan.mode is MODE
    assert plan.timezone == "Asia/Manila"
    assert plan.queued == 3
    assert plan.since == ENQUEUED_AT - BATCH_SINCE_MARGIN
    assert BATCH_SINCE_MARGIN == timedelta(hours=1)


def test_same_folder_destination_is_skipped_silently(tmp_path: Path):
    config, _folder = _config(tmp_path)

    decision = decide_batch_sweep(
        config,
        Destination(DestinationKind.SAME_FOLDER),
        enqueued_at=ENQUEUED_AT,
        queued=3,
    )

    assert decision == BatchSweepDecision()


def test_an_unregistered_root_skips_with_a_note(tmp_path: Path):
    config, _folder = _config(tmp_path)

    decision = decide_batch_sweep(
        config,
        Destination(DestinationKind.OUTPUT_ROOT, "ghost", ""),
        enqueued_at=ENQUEUED_AT,
        queued=1,
    )

    assert decision.plan is None
    assert decision.note is not None and "ghost" in decision.note


def test_a_root_missing_on_this_machine_skips_with_a_note(tmp_path: Path):
    config, folder = _config(tmp_path)
    folder.rmdir()  # registered on another machine; not resolvable here

    decision = decide_batch_sweep(
        config, _output_root_destination(), enqueued_at=ENQUEUED_AT, queued=1
    )

    assert decision.plan is None
    assert decision.note is not None and "not available" in decision.note


def test_a_non_macos_machine_skips_silently(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config, _folder = _config(tmp_path)
    monkeypatch.setattr(date_stamp_runner, "supported", lambda: False)

    decision = decide_batch_sweep(
        config, _output_root_destination(), enqueued_at=ENQUEUED_AT, queued=1
    )

    assert decision == BatchSweepDecision()


def test_queue_drained_needs_no_active_job_and_the_batch_settled():
    drained = make_snapshot(
        workers=[make_idle_worker()],
        summary=make_summary(completed=2, skipped=1, failed=0, review=0),
    )
    busy = make_snapshot(workers=[make_idle_worker(), make_snapshot().workers[0]])

    assert settled_total(drained) == 3
    assert queue_drained(drained, baseline_settled=0, queued=3) is True
    assert queue_drained(busy, baseline_settled=0, queued=1) is False
    assert queue_drained(drained, baseline_settled=2, queued=2) is False  # only 1 settled
    assert queue_drained(drained, baseline_settled=1, queued=2) is True


def test_the_watcher_yields_the_plan_exactly_once_when_drained(tmp_path: Path):
    plan = BatchSweepPlan(
        root_id="compressed",
        folder=tmp_path,
        mode=MODE,
        timezone="Asia/Manila",
        since=ENQUEUED_AT,
        queued=1,
    )
    watcher = BatchSweepWatcher(plan=plan, baseline_settled=0)
    empty = make_snapshot(
        workers=[], summary=make_summary(completed=0, skipped=0, failed=0, review=0)
    )

    assert watcher.observe(empty) is None  # nothing settled yet
    drained = make_snapshot(
        workers=[], summary=make_summary(completed=1, skipped=0, failed=0, review=0)
    )
    assert watcher.observe(drained) is plan
    assert watcher.observe(drained) is None
    assert watcher.triggered is True


# -- background runner ------------------------------------------------------


def test_runner_runs_the_injected_sweep(qapp, tmp_path: Path):
    request = SweepRequest(folder=tmp_path, mode=MODE, timezone="Asia/Manila", since=ENQUEUED_AT)
    report = StampReport(tmp_path, MODE, "Asia/Manila", ENQUEUED_AT, False, ())
    seen: list[SweepRequest] = []

    def fake_sweep(value: SweepRequest) -> StampReport:
        seen.append(value)
        return report

    runner = DateStampRunner(pool=QThreadPool(), sweep=fake_sweep)
    finished = QSignalSpy(runner.finished)

    assert runner.start(request)
    assert runner.busy
    assert _spin_until(qapp, lambda: finished.count() == 1 and not runner.busy)
    assert seen == [request]
    assert finished.at(0)[0] == report


def test_runner_rejects_a_second_sweep_while_busy(qapp, tmp_path: Path):
    release = threading.Event()
    report = StampReport(tmp_path, MODE, "Asia/Manila", None, False, ())

    def slow_sweep(_request: SweepRequest) -> StampReport:
        release.wait(2)
        return report

    runner = DateStampRunner(pool=QThreadPool(), sweep=slow_sweep)
    finished = QSignalSpy(runner.finished)
    request = SweepRequest(folder=tmp_path, mode=MODE, timezone="Asia/Manila")

    assert runner.start(request)
    assert not runner.start(request)
    release.set()
    assert _spin_until(qapp, lambda: finished.count() == 1 and not runner.busy)


def test_runner_reports_failures(qapp, tmp_path: Path):
    def broken(_request: SweepRequest) -> StampReport:
        raise ValueError("folder disappeared")

    runner = DateStampRunner(pool=QThreadPool(), sweep=broken)
    failed = QSignalSpy(runner.failed)

    assert runner.start(SweepRequest(folder=tmp_path, mode=MODE, timezone="Asia/Manila"))
    assert _spin_until(qapp, lambda: failed.count() == 1 and not runner.busy)
    assert failed.at(0)[0] == "folder disappeared"


def _spin_until(qapp, predicate, timeout_s: float = 5.0) -> bool:
    """Process Qt events until ``predicate`` holds (QSignalSpy.wait is not reliable here)."""
    deadline = time.monotonic() + timeout_s
    while not predicate() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    qapp.processEvents()
    return predicate()


# -- monitor window wiring --------------------------------------------------


class StubMonitorPanel(QWidget):
    """Minimal MonitorPanel stand-in with the runner signal the window needs."""

    def __init__(self) -> None:
        super().__init__()
        self.runner = FakeMonitorRunner()
        self.refreshes = 0

    def snapshot(self) -> None:
        return None

    def refresh_now(self) -> None:
        self.refreshes += 1

    def has_active_jobs(self) -> bool:
        return False

    def shutdown(self) -> None:
        pass


class StubConvertPanel(QWidget):
    """Minimal ConvertPanel stand-in with the decision the window consumes."""

    enqueued = Signal(int)
    roots_changed = Signal()

    def __init__(self, decision: BatchSweepDecision | None) -> None:
        super().__init__()
        self.last_batch = decision

    def refresh(self) -> None:
        pass


class StubNodesPanel(QWidget):
    """Minimal NodesPanel stand-in for the window constructor."""

    def refresh_list(self) -> None:
        pass

    def refresh_registry(self) -> None:
        pass

    def shutdown(self) -> None:
        pass


class FakeDateRunner(QObject):
    """Synchronous stand-in for ``DateStampRunner`` with the same signals and API."""

    started = Signal()
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.busy = False
        self.requests: list[SweepRequest] = []

    def start(self, request: SweepRequest) -> bool:
        if self.busy:
            return False
        self.busy = True
        self.requests.append(request)
        self.started.emit()
        return True

    def emit_finished(self, report: StampReport) -> None:
        self.busy = False
        self.finished.emit(report)

    def emit_failed(self, message: str) -> None:
        self.busy = False
        self.failed.emit(message)


def _window(tmp_path: Path, convert: StubConvertPanel, date_runner: FakeDateRunner):
    return monitor_window.MonitorWindow(
        config=AppConfig(),
        config_path=tmp_path / "config.toml",
        manifest_path=tmp_path / "manifest.sqlite",
        session=FakeControllerSession(),  # type: ignore[arg-type]
        library=make_library(tmp_path, AppConfig(), FakeScanRunner()),
        panel=StubMonitorPanel(),  # type: ignore[arg-type]
        convert=convert,  # type: ignore[arg-type]
        nodes=StubNodesPanel(),  # type: ignore[arg-type]
        date_runner=date_runner,  # type: ignore[arg-type]
    )


@pytest.fixture
def window_factory(qapp, tmp_path: Path):
    created: list[QWidget] = []

    def build(decision: BatchSweepDecision | None):
        convert = StubConvertPanel(decision)
        date_runner = FakeDateRunner()
        window = _window(tmp_path, convert, date_runner)
        created.append(window)
        return window, date_runner

    yield build

    for widget in created:
        dispose(widget)


def test_enqueue_then_drain_starts_the_sweep(window_factory, tmp_path: Path):
    plan = BatchSweepPlan(
        root_id="compressed",
        folder=tmp_path / "compressed",
        mode=MODE,
        timezone="Asia/Manila",
        since=ENQUEUED_AT - BATCH_SINCE_MARGIN,
        queued=2,
    )
    window, date_runner = window_factory(BatchSweepDecision(plan=plan))

    window.convert.enqueued.emit(2)
    window.monitor.runner.emit_updated(
        make_snapshot(
            workers=[make_idle_worker()],
            summary=make_summary(completed=2, skipped=0, failed=0, review=0),
        )
    )

    assert date_runner.requests == [
        SweepRequest(
            folder=plan.folder,
            mode=MODE,
            timezone="Asia/Manila",
            since=plan.since,
        )
    ]
    assert "creation dates: checking" in window.statusBar().currentMessage()


def test_an_armed_sweep_waits_for_the_queue(window_factory):
    plan = BatchSweepPlan(
        root_id="compressed",
        folder=Path("/nowhere"),
        mode=MODE,
        timezone="Asia/Manila",
        since=ENQUEUED_AT,
        queued=2,
    )
    window, date_runner = window_factory(BatchSweepDecision(plan=plan))

    window.convert.enqueued.emit(2)
    window.monitor.runner.emit_updated(
        make_snapshot(
            workers=[make_idle_worker()],
            summary=make_summary(completed=1, skipped=0, failed=0, review=0),
        )
    )

    assert date_runner.requests == []  # the batch has not settled yet


def test_a_skip_note_reaches_the_status_area(window_factory):
    decision = BatchSweepDecision(note="creation dates: output root 'ghost' is not registered")
    window, date_runner = window_factory(decision)

    window.convert.enqueued.emit(1)

    assert date_runner.requests == []
    assert "not registered" in window.statusBar().currentMessage()


def test_a_finished_sweep_is_summarized_in_the_status_area(window_factory, tmp_path: Path):
    window, date_runner = window_factory(None)
    report = StampReport(
        folder=tmp_path,
        mode=MODE,
        timezone="Asia/Manila",
        since=None,
        dry_run=False,
        results=(StampResult(tmp_path / "A.MP4", StampStatus.STAMPED),),
    )

    date_runner.emit_finished(report)

    assert window.statusBar().currentMessage() == "creation dates: 1 stamped, 0 failed"


def test_a_failed_sweep_is_reported_without_a_dialog(window_factory):
    window, date_runner = window_factory(None)

    date_runner.emit_failed("folder disappeared")

    assert window.statusBar().currentMessage() == "creation dates: folder disappeared"
