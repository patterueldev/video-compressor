"""Nodes runner: one background operation at a time, signals, stop (issue #54)."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QThreadPool
from PySide6.QtTest import QSignalSpy

from videocompressor.gui.nodes_runner import NodesRunner


def _spin_until(qapp, predicate: Callable[[], bool], timeout_s: float = 5.0) -> bool:
    """Process Qt events until ``predicate`` holds (QSignalSpy.wait is not reliable here)."""
    deadline = time.monotonic() + timeout_s
    while not predicate() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    qapp.processEvents()
    return predicate()


def test_runner_reports_started_and_finished(qapp):
    runner = NodesRunner(pool=QThreadPool())
    started = QSignalSpy(runner.started)
    finished = QSignalSpy(runner.finished)
    runner.start()

    assert runner.submit("probe node-a", lambda: "activity") is True
    assert _spin_until(qapp, lambda: finished.count() == 1 and not runner.busy)

    assert started.count() == 1
    assert started.at(0)[0] == "probe node-a"
    assert finished.at(0)[0] == "activity"
    runner.stop()


def test_runner_rejects_a_second_operation_while_busy(qapp):
    release = threading.Event()
    calls: list[int] = []

    def slow() -> str:
        calls.append(1)
        release.wait(2)
        return "done"

    runner = NodesRunner(pool=QThreadPool())
    runner.start()
    assert runner.submit("one", slow) is True
    assert runner.submit("two", lambda: "ignored") is False
    release.set()
    finished = QSignalSpy(runner.finished)

    assert _spin_until(qapp, lambda: finished.count() == 1 and not runner.busy)
    assert calls == [1]
    runner.stop()


def test_runner_reports_failures(qapp):
    def broken() -> str:
        raise RuntimeError("boom")

    runner = NodesRunner(pool=QThreadPool())
    runner.start()
    failed = QSignalSpy(runner.failed)

    assert runner.submit("probe node-a", broken) is True
    assert _spin_until(qapp, lambda: failed.count() == 1 and not runner.busy)
    assert failed.at(0)[0] == "boom"
    runner.stop()


def test_runner_refuses_operations_once_stopped(qapp):
    runner = NodesRunner(pool=QThreadPool())
    runner.start()
    runner.stop()

    assert runner.submit("probe node-a", lambda: "late") is False


def test_runner_drops_a_result_after_stop(qapp):
    release = threading.Event()

    def slow() -> str:
        release.wait(2)
        return "late"

    runner = NodesRunner(pool=QThreadPool())
    runner.start()
    finished = QSignalSpy(runner.finished)

    assert runner.submit("probe node-a", slow) is True
    runner.stop()
    release.set()

    assert _spin_until(qapp, lambda: not runner.busy)
    qapp.processEvents()
    assert finished.count() == 0
