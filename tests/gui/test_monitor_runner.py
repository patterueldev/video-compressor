"""Monitor runner: background polling, busy guard, and failure reporting (issue #9)."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QThreadPool
from PySide6.QtTest import QSignalSpy

from gui.monitor_support import make_snapshot
from videocompressor.controller.protocol import ControllerError
from videocompressor.gui.monitor_runner import MonitorRunner


def _spin_until(qapp, predicate: Callable[[], bool], timeout_s: float = 5.0) -> bool:
    """Process Qt events until ``predicate`` holds (QSignalSpy.wait is not reliable here)."""
    deadline = time.monotonic() + timeout_s
    while not predicate() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    qapp.processEvents()
    return predicate()


def test_runner_polls_in_the_background(qapp):
    calls: list[int] = []

    def fetch():
        calls.append(1)
        return make_snapshot()

    runner = MonitorRunner(fetch, pool=QThreadPool(), interval_ms=60_000)
    updated = QSignalSpy(runner.updated)
    runner.start()

    assert _spin_until(qapp, lambda: updated.count() == 1 and not runner.busy)
    assert updated.at(0)[0].workers[0].worker_id == "homeserver"
    runner.stop()
    assert len(calls) == 1


def test_runner_rejects_a_second_fetch_while_busy(qapp):
    release = threading.Event()
    calls: list[int] = []

    def slow_fetch():
        calls.append(1)
        release.wait(2)
        return make_snapshot()

    runner = MonitorRunner(slow_fetch, pool=QThreadPool(), interval_ms=60_000)
    updated = QSignalSpy(runner.updated)
    runner.start()
    runner.refresh_now()  # one fetch is already in flight
    release.set()

    assert _spin_until(qapp, lambda: updated.count() == 1 and not runner.busy)
    assert len(calls) == 1
    runner.stop()


def test_runner_reports_failures(qapp):
    def broken():
        raise ControllerError(404, "unknown_worker", "worker 'ghost' is not registered")

    runner = MonitorRunner(broken, pool=QThreadPool(), interval_ms=60_000)
    failed = QSignalSpy(runner.failed)
    runner.start()

    assert _spin_until(qapp, lambda: failed.count() == 1 and not runner.busy)
    assert failed.at(0)[0] == "worker 'ghost' is not registered"
    runner.stop()


def test_runner_stops_polling(qapp):
    calls: list[int] = []

    def fetch():
        calls.append(1)
        return make_snapshot()

    runner = MonitorRunner(fetch, pool=QThreadPool(), interval_ms=10)
    updated = QSignalSpy(runner.updated)
    runner.start()
    assert _spin_until(qapp, lambda: updated.count() >= 1)

    runner.stop()
    count = updated.count()
    runner.refresh_now()  # stopped: ignored
    time.sleep(0.05)
    qapp.processEvents()
    assert updated.count() == count
