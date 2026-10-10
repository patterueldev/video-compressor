"""Monitor window: tabs, close guards, and session teardown (issue #9)."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QMessageBox, QWidget

from gui.monitor_support import FakeSession
from gui.monitor_support_qt import FakeScanRunner, make_library
from videocompressor.core.config import AppConfig
from videocompressor.gui import monitor_window


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

    def has_active_jobs(self) -> bool:
        return self.active

    def shutdown(self) -> None:
        self.shutdowns += 1


def _build(tmp_path: Path, *, hosted: bool = True, panel_active: bool = False, busy: bool = False):
    session = FakeSession(hosted=hosted)
    panel = FakePanel(panel_active)
    library_runner = FakeScanRunner()
    library_runner.busy = busy
    window = monitor_window.MonitorWindow(
        config=AppConfig(),
        config_path=tmp_path / "config.toml",
        manifest_path=tmp_path / "manifest.sqlite",
        session=session,  # type: ignore[arg-type]
        library=make_library(tmp_path, AppConfig(), library_runner),
        panel=panel,  # type: ignore[arg-type]
    )
    return window, session, panel, library_runner


def test_window_shows_library_and_monitor_tabs(qapp, tmp_path):
    window, _session, _panel, _runner = _build(tmp_path)
    assert window.tabs.count() == 2
    assert window.tabs.tabText(0) == "Library"
    assert window.tabs.tabText(1) == "Monitor"
    assert window.tabs.widget(0) is window.library
    assert window.tabs.widget(1) is window.monitor


def test_close_is_refused_while_a_scan_runs(qapp, tmp_path, monkeypatch):
    box = BoxSpy()
    monkeypatch.setattr(monitor_window, "QMessageBox", box)
    window, session, panel, _runner = _build(tmp_path, hosted=False, busy=True)

    event = QCloseEvent()
    window.closeEvent(event)

    assert not event.isAccepted()
    assert box.infos  # the user is told why
    assert session.closed == 0
    assert panel.shutdowns == 0


def test_hosted_close_with_an_active_job_asks_first(qapp, tmp_path, monkeypatch):
    box = BoxSpy(answer=QMessageBox.StandardButton.No)
    monkeypatch.setattr(monitor_window, "QMessageBox", box)
    window, session, panel, _runner = _build(tmp_path, hosted=True, panel_active=True)

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


def test_attached_close_needs_no_prompt(qapp, tmp_path, monkeypatch):
    box = BoxSpy()
    monkeypatch.setattr(monitor_window, "QMessageBox", box)
    window, session, panel, _runner = _build(tmp_path, hosted=False, panel_active=True)

    event = QCloseEvent()
    window.closeEvent(event)

    assert event.isAccepted()
    assert box.questions == []
    assert session.closed == 1
    assert panel.shutdowns == 1


def test_window_builds_the_monitor_panel_from_the_session(qapp, tmp_path):
    session = FakeSession(hosted=True, description="hosting controller at http://127.0.0.1:9999")
    window = monitor_window.MonitorWindow(
        config=AppConfig(),
        config_path=tmp_path / "config.toml",
        manifest_path=tmp_path / "manifest.sqlite",
        session=session,  # type: ignore[arg-type]
        library=make_library(tmp_path, AppConfig(), FakeScanRunner()),
    )
    try:
        assert window.monitor.description_label.text() == (
            "hosting controller at http://127.0.0.1:9999"
        )
    finally:
        window.monitor.shutdown()
