"""Monitor window: tabs, close guards, and session teardown (issue #9)."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QMessageBox, QWidget

from gui.monitor_support import (
    FakeControllerSession,
    FakeMonitorClient,
    worker_payload,
    workers_payload,
)
from gui.monitor_support_qt import FakeScanRunner, dispose, make_library
from videocompressor.core.config import AppConfig
from videocompressor.gui import monitor_window
from videocompressor.gui.controller_session import start_controller_session
from videocompressor.gui.monitor_service import MonitorSession


class BoxSpy:
    """Records QMessageBox calls instead of opening dialogs."""

    StandardButton = QMessageBox.StandardButton

    def __init__(self, answer: QMessageBox.StandardButton = QMessageBox.StandardButton.Yes) -> None:
        self.answer = answer
        self.infos: list[str] = []
        self.questions: list[str] = []

    def information(self, _parent, _title, text) -> None:
        self.infos.append(text)

    def question(self, _parent, _title, text) -> QMessageBox.StandardButton:
        self.questions.append(text)
        return self.answer


class FakePanel(QWidget):
    """Stand-in for ``MonitorPanel`` in the window tests."""

    def __init__(self, active: bool = False) -> None:
        super().__init__()
        self.active = active
        self.shutdowns = 0
        self.refreshes = 0

    def has_active_jobs(self) -> bool:
        return self.active

    def refresh_now(self) -> None:
        self.refreshes += 1

    def shutdown(self) -> None:
        self.shutdowns += 1


class FakeConvertPanel(QWidget):
    """Stand-in for ``ConvertPanel`` in the window tests."""

    enqueued = Signal(int)

    def __init__(self) -> None:
        super().__init__()
        self.refreshes = 0

    def refresh(self) -> None:
        self.refreshes += 1


def _spin_until(qapp, predicate: Callable[[], bool], timeout_s: float = 5.0) -> bool:
    """Process Qt events until ``predicate`` holds (QSignalSpy.wait is not reliable here)."""
    deadline = time.monotonic() + timeout_s
    while not predicate() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    qapp.processEvents()
    return predicate()


def _build(tmp_path: Path, *, hosted: bool = True, panel_active: bool = False, busy: bool = False):
    session = FakeControllerSession(hosted=hosted)
    panel = FakePanel(panel_active)
    convert = FakeConvertPanel()
    library_runner = FakeScanRunner()
    library_runner.busy = busy
    window = monitor_window.MonitorWindow(
        config=AppConfig(),
        config_path=tmp_path / "config.toml",
        manifest_path=tmp_path / "manifest.sqlite",
        session=session,  # type: ignore[arg-type]
        library=make_library(tmp_path, AppConfig(), library_runner),
        panel=panel,  # type: ignore[arg-type]
        convert=convert,  # type: ignore[arg-type]
    )
    return window, session, panel, library_runner


def _window_with_default_panel(tmp_path: Path, session: object) -> monitor_window.MonitorWindow:
    return monitor_window.MonitorWindow(
        config=AppConfig(),
        config_path=tmp_path / "config.toml",
        manifest_path=tmp_path / "manifest.sqlite",
        session=session,  # type: ignore[arg-type]
        library=make_library(tmp_path, AppConfig(), FakeScanRunner()),
    )


def test_window_shows_convert_library_and_monitor_tabs(qapp, tmp_path):
    window, _session, _panel, _runner = _build(tmp_path)
    try:
        assert window.tabs.count() == 3
        assert window.tabs.tabText(0) == "Convert"
        assert window.tabs.tabText(1) == "Library"
        assert window.tabs.tabText(2) == "Monitor"
        assert window.tabs.widget(0) is window.convert
        assert window.tabs.widget(1) is window.library
        assert window.tabs.widget(2) is window.monitor
        assert window.tabs.currentWidget() is window.convert
    finally:
        dispose(window)


def test_enqueue_switches_to_the_monitor_and_refreshes(qapp, tmp_path):
    window, _session, panel, _runner = _build(tmp_path)
    try:
        window.convert.enqueued.emit(2)

        assert window.tabs.currentWidget() is window.monitor
        assert panel.refreshes == 1
    finally:
        dispose(window)


def test_switching_to_the_convert_tab_refreshes_it(qapp, tmp_path):
    window, _session, _panel, _runner = _build(tmp_path)
    try:
        window.tabs.setCurrentWidget(window.library)
        window.tabs.setCurrentWidget(window.convert)

        assert window.convert.refreshes == 1
    finally:
        dispose(window)


def test_close_is_refused_while_a_scan_runs(qapp, tmp_path, monkeypatch):
    box = BoxSpy()
    monkeypatch.setattr(monitor_window, "QMessageBox", box)
    window, session, panel, _runner = _build(tmp_path, hosted=False, busy=True)
    try:
        event = QCloseEvent()
        window.closeEvent(event)

        assert not event.isAccepted()
        assert box.infos  # the user is told why
        assert session.closed == 0
        assert panel.shutdowns == 0
    finally:
        dispose(window)


def test_hosted_close_with_an_active_job_asks_first(qapp, tmp_path, monkeypatch):
    box = BoxSpy(answer=QMessageBox.StandardButton.No)
    monkeypatch.setattr(monitor_window, "QMessageBox", box)
    window, session, panel, _runner = _build(tmp_path, hosted=True, panel_active=True)
    try:
        refused = QCloseEvent()
        window.closeEvent(refused)
        assert not refused.isAccepted()
        assert box.questions
        assert session.closed == 0
        assert panel.shutdowns == 0

        box.answer = QMessageBox.StandardButton.Yes
        accepted = QCloseEvent()
        window.closeEvent(accepted)
        assert accepted.isAccepted()
        assert session.closed == 1
        assert panel.shutdowns == 1
    finally:
        dispose(window)


def test_attached_close_needs_no_prompt(qapp, tmp_path, monkeypatch):
    box = BoxSpy()
    monkeypatch.setattr(monitor_window, "QMessageBox", box)
    window, session, panel, _runner = _build(tmp_path, hosted=False, panel_active=True)
    try:
        event = QCloseEvent()
        window.closeEvent(event)

        assert event.isAccepted()
        assert box.questions == []
        assert session.closed == 1
        assert panel.shutdowns == 1
    finally:
        dispose(window)


def test_window_wires_a_monitor_session_into_the_panel(qapp, tmp_path):
    """Regression for RF-1: the panel must poll a MonitorSession, not the ControllerSession."""
    config = AppConfig()
    config.controller.port = 0
    config.controller.token = "window-test-token"
    controller = start_controller_session(
        config,
        config_path=tmp_path / "config.toml",
        store_path=tmp_path / "controller.sqlite",
        manifest_path=tmp_path / "manifest.sqlite",
    )
    try:
        window = _window_with_default_panel(tmp_path, controller)
        try:
            assert isinstance(window.monitor.session, MonitorSession)
            assert window.monitor.description_label.text() == controller.description
            assert _spin_until(
                qapp, lambda: window.monitor.status_label.text().startswith("updated")
            )
            window.monitor.runner.stop()  # serialize the direct fetch below
            assert _spin_until(qapp, lambda: not window.monitor.runner.busy)
            snapshot = window.monitor.session.snapshot()  # AttributeError before the fix
            assert snapshot.workers == ()
        finally:
            window.monitor.shutdown()
            dispose(window)
    finally:
        controller.close()


def test_window_builds_the_panel_from_the_controller_session_client(qapp, tmp_path):
    client = FakeMonitorClient(workers=workers_payload(worker_payload()))
    controller = FakeControllerSession(client=client)
    window = _window_with_default_panel(tmp_path, controller)
    try:
        assert isinstance(window.monitor.session, MonitorSession)
        assert window.monitor.description_label.text() == controller.description
        snapshot = window.monitor.session.snapshot()  # AttributeError before the fix
        assert snapshot.workers[0].worker_id == "homeserver"
    finally:
        window.monitor.shutdown()
        dispose(window)
