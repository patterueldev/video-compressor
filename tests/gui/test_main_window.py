"""Main window: drop flow, dry-run wiring, errors, and the close guard (issue #8)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QCloseEvent

from gui.support import make_report, make_review_row, make_scan_row
from videocompressor.core.config import AppConfig, register_root
from videocompressor.core.models import TemplateSettings
from videocompressor.gui import main_window


class FakeRunner(QObject):
    """Synchronous stand-in for ScanRunner with the same signals and API."""

    started = Signal()
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.busy = False
        self.requests = []

    def start(self, request) -> bool:
        self.busy = True
        self.requests.append(request)
        self.started.emit()
        return True

    def complete(self, report) -> None:
        self.busy = False
        self.finished.emit(report)

    def fail(self, message: str) -> None:
        self.busy = False
        self.failed.emit(message)


class BoxSpy:
    """Records QMessageBox calls instead of opening dialogs."""

    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.infos: list[str] = []

    def warning(self, _parent, _title, text) -> None:
        self.warnings.append(text)

    def information(self, _parent, _title, text) -> None:
        self.infos.append(text)


@pytest.fixture
def message_box(monkeypatch) -> BoxSpy:
    spy = BoxSpy()
    monkeypatch.setattr(main_window, "QMessageBox", spy)
    return spy


@pytest.fixture
def window(tmp_path, qapp):
    config = AppConfig()
    root_dir = tmp_path / "archive"
    folder = root_dir / "2026-06-Ride"
    folder.mkdir(parents=True)
    register_root(config, root_id="camera", name="Camera", path=root_dir)
    runner = FakeRunner()
    win = main_window.MainWindow(
        config=config,
        config_path=tmp_path / "config.toml",
        manifest_path=tmp_path / "manifest.sqlite",
        runner=runner,
    )
    return win, runner, folder


def test_window_starts_without_a_selection(window):
    win, _runner, _folder = window
    assert not win.run_button.isEnabled()
    assert win.clip_tables.summary_text() == "No dry run yet"


def test_drop_on_a_registered_root_scans_and_shows_rows(window):
    win, runner, folder = window

    win.on_folder_dropped(folder)

    (request,) = runner.requests
    assert request.root.id == "camera"
    assert request.folder == folder
    assert request.template_id == "motorcycle"
    assert request.manifest_path == win.manifest_path
    assert "camera" in win.selection_label.text()
    assert not win.run_button.isEnabled()  # disabled while scanning

    runner.complete(
        make_report(
            [make_scan_row("2026-06-Ride/GS010001.MP4"), make_review_row()],
            folder_relative="2026-06-Ride",
        )
    )

    assert win.run_button.isEnabled()
    assert win.clip_tables.queue_model.rowCount() == 1
    assert win.clip_tables.review_model.rowCount() == 1
    assert "2 clips" in win.clip_tables.summary_text()


def test_drop_outside_roots_prompts_registration(window, tmp_path):
    win, runner, _folder = window
    outside = tmp_path / "other" / "ride"
    outside.mkdir(parents=True)

    def register(parent_folder: Path):
        return register_root(win.config, root_id="other", name="Other", path=parent_folder)

    win.roots_panel.prompt_register = register
    win.on_folder_dropped(outside)

    (request,) = runner.requests
    assert request.root.id == "other"
    assert request.folder == outside


def test_cancelled_registration_ignores_the_drop(window, tmp_path):
    win, runner, _folder = window
    outside = tmp_path / "other" / "ride"
    outside.mkdir(parents=True)

    win.roots_panel.prompt_register = lambda folder: None
    win.on_folder_dropped(outside)

    assert runner.requests == []
    assert not win.run_button.isEnabled()


def test_registration_of_a_non_containing_root_ignores_the_drop(window, tmp_path):
    win, runner, _folder = window
    outside = tmp_path / "other" / "ride"
    outside.mkdir(parents=True)
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()

    def register(_folder: Path):
        return register_root(win.config, root_id="unrelated", name="Unrelated", path=unrelated)

    win.roots_panel.prompt_register = register
    win.on_folder_dropped(outside)

    assert runner.requests == []
    assert not win.run_button.isEnabled()


def test_scan_failure_shows_a_dialog(window, message_box):
    win, runner, folder = window
    win.on_folder_dropped(folder)

    runner.fail("ffprobe was not found; install FFmpeg and try again")

    assert message_box.warnings == ["ffprobe was not found; install FFmpeg and try again"]
    assert win.run_button.isEnabled()


def test_close_is_refused_while_scanning(window, message_box):
    win, runner, folder = window
    win.on_folder_dropped(folder)
    assert runner.busy

    refused = QCloseEvent()
    win.closeEvent(refused)
    assert not refused.isAccepted()
    assert message_box.infos  # the user is told why

    runner.busy = False
    allowed = QCloseEvent()
    win.closeEvent(allowed)
    assert allowed.isAccepted()


def test_template_change_marks_the_displayed_scan_stale(window):
    win, runner, folder = window
    win.on_folder_dropped(folder)
    runner.complete(make_report([make_scan_row()]))
    assert win.clip_tables.stale_notice() == ""

    win.config.templates["motorcycle"] = replace(
        TemplateSettings(), video_bitrate_kbps=1500
    )
    win.templates_panel.template_selected.emit("motorcycle")

    assert "run the dry run again" in win.clip_tables.stale_notice()
    assert win.clip_tables.state().rows()[0].video_bitrate_kbps == 1500
