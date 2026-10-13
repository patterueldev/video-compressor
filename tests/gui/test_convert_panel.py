"""Convert panel: selection, destination validation, and enqueue (issue #41)."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6")

import shiboken6
from PySide6.QtWidgets import QWidget

from videocompressor.core.config import AppConfig, add_template, register_root
from videocompressor.gui import convert_panel
from videocompressor.gui.monitor_client import MonitorUnavailable


class FakeEnqueueClient:
    """Records payloads and returns a canned response (or raises)."""

    def __init__(self, response: dict | None = None, error: Exception | None = None) -> None:
        self.payloads: list[dict] = []
        self.response = response if response is not None else _empty_response()
        self.error = error

    def enqueue(self, payload) -> dict:
        self.payloads.append(payload)
        if self.error is not None:
            raise self.error
        return self.response


class FakeFileDialog:
    """Replaces ``QFileDialog`` so the dialog wiring is exercised hermetically."""

    files: tuple[str, ...] = ()
    folder: str = ""

    @classmethod
    def getOpenFileNames(cls, *_args, **_kwargs):
        return list(cls.files), "Videos (*.mp4 *.mov)"

    @classmethod
    def getExistingDirectory(cls, *_args, **_kwargs):
        return cls.folder


class BoxSpy:
    """Records QMessageBox calls instead of opening dialogs."""

    def __init__(self) -> None:
        self.warnings: list[str] = []

    def warning(self, _parent, _title, text) -> None:
        self.warnings.append(text)


def _empty_response() -> dict:
    return {"jobs": [], "queued": 0, "skipped": 0, "rejected": 0}


def clip(path: Path, payload: bytes = b"x" * 64) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


@pytest.fixture
def panel_factory(qapp, tmp_path):
    """Build a panel rooted in ``tmp_path``; delete the widgets after the test.

    Qt widgets left to Python's garbage collector can be destroyed while the
    controller's background threads run (see ``monitor_support_qt.dispose``),
    which crashes the process; deleting them deterministically avoids that.
    """
    created: list[QWidget] = []

    def build(*, client: FakeEnqueueClient | None = None):
        config = AppConfig()
        root_dir = tmp_path / "archive"
        root_dir.mkdir(exist_ok=True)
        register_root(config, root_id="archive", name="Archive", path=root_dir)
        client = client if client is not None else FakeEnqueueClient()
        panel = convert_panel.ConvertPanel(client, config, tmp_path / "config.toml")
        created.append(panel)
        return panel, client, config

    yield build

    for widget in created:
        if shiboken6.isValid(widget):
            shiboken6.delete(widget)


@pytest.fixture
def message_box(monkeypatch) -> BoxSpy:
    spy = BoxSpy()
    monkeypatch.setattr(convert_panel, "QMessageBox", spy)
    return spy


@pytest.fixture
def file_dialog(monkeypatch) -> type[FakeFileDialog]:
    FakeFileDialog.files = ()
    FakeFileDialog.folder = ""
    monkeypatch.setattr(convert_panel, "QFileDialog", FakeFileDialog)
    return FakeFileDialog


# -- selection ---------------------------------------------------------------


def test_delete_defaults_off_with_explanation(qapp, tmp_path, panel_factory):
    panel, _client, _config = panel_factory()

    assert panel.delete_checkbox.isChecked() is False
    assert panel.delete_checkbox.isEnabled()
    assert "permanently deleted" in panel.delete_hint.text()


def test_drop_a_folder_adds_only_videos_with_counts_and_size(qapp, tmp_path, panel_factory):
    panel, _client, config = panel_factory()
    root = config.roots[0].path
    clip(root / "RIDE" / "GS010001.MP4", b"a" * 1024)
    clip(root / "RIDE" / "GS010002.MP4", b"b" * 2048)
    (root / "RIDE" / "notes.txt").write_text("hi", encoding="utf-8")

    panel.drop_zone.paths_dropped.emit([root / "RIDE"])

    assert panel.pending_table.rowCount() == 2
    assert "2 files" in panel.selection_label.text()
    assert "KB" in panel.selection_label.text()
    assert "added 2 videos" in panel.message_label.text()
    assert "ignored 1 non-video file" in panel.message_label.text()
    assert panel.start_button.isEnabled()


def test_open_files_dialog_adds_rows(qapp, tmp_path, panel_factory, file_dialog):
    panel, _client, config = panel_factory()
    video = clip(config.roots[0].path / "A.MP4")
    file_dialog.files = (str(video),)

    panel.open_files()

    assert panel.pending_table.rowCount() == 1
    assert panel.pending_table.item(0, 0).text() == "A.MP4"


def test_open_folder_dialog_recurses(qapp, tmp_path, panel_factory, file_dialog):
    panel, _client, config = panel_factory()
    folder = config.roots[0].path / "RIDE"
    clip(folder / "A.MP4")
    clip(folder / "B.MOV")
    file_dialog.folder = str(folder)

    panel.open_folder()

    assert panel.pending_table.rowCount() == 2


def test_cancelled_dialogs_add_nothing(qapp, tmp_path, panel_factory, file_dialog):
    panel, _client, _config = panel_factory()

    panel.open_files()
    panel.open_folder()

    assert panel.pending_table.rowCount() == 0


def test_clear_resets_the_selection(qapp, tmp_path, panel_factory):
    panel, _client, config = panel_factory()
    panel.add_paths([clip(config.roots[0].path / "A.MP4")])

    panel.clear_selection()

    assert panel.pending_table.rowCount() == 0
    assert "no videos selected" in panel.selection_label.text()
    assert not panel.start_button.isEnabled()


# -- destination validation --------------------------------------------------


def test_start_blocked_without_selection(qapp, tmp_path, panel_factory, message_box):
    panel, client, _config = panel_factory()

    panel.start()

    assert client.payloads == []
    assert "select at least one video" in panel.message_label.text()
    assert message_box.warnings


def test_subfolder_validation_blocks_start(qapp, tmp_path, panel_factory, message_box):
    panel, client, config = panel_factory()
    panel.add_paths([clip(config.roots[0].path / "A.MP4")])

    panel.subfolder_edit.setText("../escape")

    assert "subfolder" in panel.destination_hint.text()
    assert not panel.start_button.isEnabled()
    panel.start()
    assert client.payloads == []
    assert "subfolder" in panel.message_label.text()


def test_custom_folder_inside_a_root_is_valid(qapp, tmp_path, panel_factory):
    panel, _client, config = panel_factory()
    panel.add_paths([clip(config.roots[0].path / "A.MP4")])
    panel.mode_combo.setCurrentIndex(1)

    panel.folder_edit.setText(str(config.roots[0].path / "Out" / "2026-10"))

    destination, problem = panel.destination.destination()
    assert problem is None
    assert destination.root_id == "archive"
    assert destination.relative_path == "Out/2026-10"
    assert panel.start_button.isEnabled()


def test_same_folder_forces_delete_and_explains(qapp, tmp_path, panel_factory):
    panel, _client, _config = panel_factory()

    panel.mode_combo.setCurrentIndex(2)  # Same folder as source

    assert panel.delete_checkbox.isChecked()
    assert not panel.delete_checkbox.isEnabled()
    assert "cannot be kept" in panel.delete_hint.text()
    assert "replaces the source" in panel.destination_hint.text()

    panel.mode_combo.setCurrentIndex(0)  # back to a registered output root

    assert panel.delete_checkbox.isEnabled()
    assert not panel.delete_checkbox.isChecked()
    assert "permanently deleted" in panel.delete_hint.text()


def test_a_manual_delete_choice_survives_the_same_folder_mode(qapp, tmp_path, panel_factory):
    panel, _client, _config = panel_factory()

    panel.delete_checkbox.setChecked(True)
    panel.mode_combo.setCurrentIndex(2)
    panel.mode_combo.setCurrentIndex(0)

    assert panel.delete_checkbox.isChecked()


# -- enqueue -----------------------------------------------------------------


def test_start_enqueues_and_reports_statuses(qapp, tmp_path, panel_factory):
    panel, client, config = panel_factory()
    root = config.roots[0].path
    panel.add_paths([clip(root / "A.MP4"), clip(root / "B.MP4")])
    client.response = {
        "jobs": [
            {"root_id": "archive", "relative_path": "A.MP4", "status": "queued", "reason": None},
            {
                "root_id": "archive",
                "relative_path": "B.MP4",
                "status": "skipped",
                "reason": "already_complete",
            },
        ],
        "queued": 1,
        "skipped": 1,
        "rejected": 0,
    }
    queued: list[int] = []
    panel.enqueued.connect(queued.append)

    panel.start()

    (payload,) = client.payloads
    assert payload == {
        "files": [
            {"root_id": "archive", "relative_path": "A.MP4"},
            {"root_id": "archive", "relative_path": "B.MP4"},
        ],
        "destination": {"kind": "output_root", "root_id": "archive"},
        "template_id": "motorcycle",
        "delete_original": False,
    }
    assert queued == [1]
    assert "queued 1 of 2 clips" in panel.message_label.text()
    assert panel.pending_table.item(0, 2).text() == "queued"
    assert panel.pending_table.item(1, 2).text() == "skipped: already complete"


def test_same_folder_start_sends_delete_true(qapp, tmp_path, panel_factory):
    panel, client, config = panel_factory()
    panel.add_paths([clip(config.roots[0].path / "A.MP4")])
    panel.mode_combo.setCurrentIndex(2)
    client.response = {
        "jobs": [
            {"root_id": "archive", "relative_path": "A.MP4", "status": "queued", "reason": None}
        ],
        "queued": 1,
        "skipped": 0,
        "rejected": 0,
    }

    panel.start()

    (payload,) = client.payloads
    assert payload["destination"] == {"kind": "same_folder"}
    assert payload["delete_original"] is True


def test_all_skipped_batch_emits_nothing(qapp, tmp_path, panel_factory):
    panel, client, config = panel_factory()
    panel.add_paths([clip(config.roots[0].path / "A.MP4")])
    client.response = {
        "jobs": [
            {
                "root_id": "archive",
                "relative_path": "A.MP4",
                "status": "skipped",
                "reason": "already_queued",
            }
        ],
        "queued": 0,
        "skipped": 1,
        "rejected": 0,
    }
    queued: list[int] = []
    panel.enqueued.connect(queued.append)

    panel.start()

    assert queued == []
    assert "queued 0 of 1 clips" in panel.message_label.text()


def test_enqueue_error_shows_warning_and_no_signal(qapp, tmp_path, panel_factory, message_box):
    client = FakeEnqueueClient(error=MonitorUnavailable("controller unreachable at http://x"))
    panel, client, config = panel_factory(client=client)
    panel.add_paths([clip(config.roots[0].path / "A.MP4")])
    queued: list[int] = []
    panel.enqueued.connect(queued.append)

    panel.start()

    assert queued == []
    assert "unreachable" in panel.message_label.text()
    assert message_box.warnings and "unreachable" in message_box.warnings[0]


def test_refresh_picks_up_new_roots_and_templates(qapp, tmp_path, panel_factory):
    panel, _client, config = panel_factory()
    new_root = tmp_path / "compressed"
    new_root.mkdir()
    register_root(config, root_id="compressed", name="Compressed", path=new_root)
    add_template(config, "vlog")

    panel.refresh()

    assert panel.root_combo.findData("compressed") >= 0
    assert panel.template_combo.findText("vlog") >= 0


def test_template_choice_goes_into_the_payload(qapp, tmp_path, panel_factory):
    panel, client, config = panel_factory()
    add_template(config, "vlog")
    panel.refresh()
    panel.add_paths([clip(config.roots[0].path / "A.MP4")])
    panel.template_combo.setCurrentText("vlog")
    client.response = {
        "jobs": [
            {"root_id": "archive", "relative_path": "A.MP4", "status": "queued", "reason": None}
        ],
        "queued": 1,
        "skipped": 0,
        "rejected": 0,
    }

    panel.start()

    (payload,) = client.payloads
    assert payload["template_id"] == "vlog"
