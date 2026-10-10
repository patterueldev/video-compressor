"""Scan runner: background execution, busy guard, and failure reporting (issue #8)."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QThreadPool
from PySide6.QtTest import QSignalSpy

from gui.support import make_report
from videocompressor.core.models import ArchiveRoot, CaptureTimeMode, TemplateSettings
from videocompressor.gui.scan_runner import ScanRunner
from videocompressor.gui.scan_service import ScanRequest, run_scan


def _spin_until(qapp, predicate, timeout_s: float = 5.0) -> bool:
    """Process Qt events until ``predicate`` holds (QSignalSpy.wait is not reliable here)."""
    deadline = time.monotonic() + timeout_s
    while not predicate() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    qapp.processEvents()
    return predicate()


def _request(tmp_path: Path) -> ScanRequest:
    root = tmp_path / "archive"
    folder = root / "ride"
    folder.mkdir(parents=True)
    return ScanRequest(
        root=ArchiveRoot(id="camera", name="Camera", path=root),
        folder=folder,
        template_id="motorcycle",
        settings=TemplateSettings(),
        capture_mode=CaptureTimeMode.LOCAL_WALL_CLOCK,
        manifest_path=None,
    )


def test_runner_scans_in_the_background(qapp, tmp_path):
    runner = ScanRunner(pool=QThreadPool())
    finished = QSignalSpy(runner.finished)

    assert runner.start(_request(tmp_path))
    assert runner.busy
    assert _spin_until(qapp, lambda: finished.count() == 1 and not runner.busy)
    report = finished.at(0)[0]
    assert report.rows == ()


def test_runner_rejects_a_second_scan_while_busy(qapp, tmp_path):
    release = threading.Event()

    def slow_scan(request):
        release.wait(2)
        return run_scan(request)

    runner = ScanRunner(pool=QThreadPool(), scan=slow_scan)
    finished = QSignalSpy(runner.finished)
    request = _request(tmp_path)

    assert runner.start(request)
    assert not runner.start(request)
    release.set()
    assert _spin_until(qapp, lambda: finished.count() == 1 and not runner.busy)


def test_runner_reports_scan_failures(qapp, tmp_path):
    def broken(request):
        raise ValueError("folder is outside archive root")

    runner = ScanRunner(pool=QThreadPool(), scan=broken)
    failed = QSignalSpy(runner.failed)
    started = QSignalSpy(runner.started)

    assert runner.start(_request(tmp_path))
    assert _spin_until(qapp, lambda: failed.count() == 1 and not runner.busy)
    assert failed.at(0)[0] == "folder is outside archive root"
    assert started.count() == 1


def test_runner_runs_the_injected_scan(qapp, tmp_path):
    seen: list[ScanRequest] = []

    def fake_scan(request):
        seen.append(request)
        return make_report()

    runner = ScanRunner(pool=QThreadPool(), scan=fake_scan)
    finished = QSignalSpy(runner.finished)
    request = _request(tmp_path)

    runner.start(request)
    assert _spin_until(qapp, lambda: finished.count() == 1)
    assert seen == [request]
