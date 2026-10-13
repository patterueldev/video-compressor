"""Root registration and removal: validation, persistence, and the guard (issues #8, #82)."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QListWidget, QMessageBox

from videocompressor.core.config import AppConfig, load_config
from videocompressor.core.manifest import Manifest
from videocompressor.core.models import ArchiveRoot, ClipRecord, Fingerprint, JobState
from videocompressor.gui import roots_panel
from videocompressor.gui.roots_panel import RootDialog, RootsPanel, mapping_reminder


@pytest.fixture
def config(tmp_path):
    return AppConfig(), tmp_path / "config.toml"


@pytest.fixture
def archive(tmp_path):
    root = tmp_path / "share" / "CAMERA"
    root.mkdir(parents=True)
    return root


class BoxSpy:
    """Records QMessageBox calls and answers question dialogs (issue #82)."""

    StandardButton = QMessageBox.StandardButton

    def __init__(self, answer: QMessageBox.StandardButton | None = None) -> None:
        self.warnings: list[str] = []
        self.questions: list[str] = []
        self.answer = QMessageBox.StandardButton.No if answer is None else answer

    def warning(self, _parent, _title, text) -> None:
        self.warnings.append(text)

    def question(self, _parent, _title, text) -> QMessageBox.StandardButton:
        self.questions.append(text)
        return self.answer


def make_clip(root_id: str, relative_path: str, state: JobState) -> ClipRecord:
    return ClipRecord(
        root_id=root_id,
        relative_path=relative_path,
        source=Fingerprint(size_bytes=10, mtime_ns=1, digest="d"),
        state=state,
    )


def test_register_from_values_persists_and_lists(config, archive, qapp):
    app_config, config_path = config
    panel = RootsPanel(app_config, config_path)
    changed: list[bool] = []
    panel.roots_changed.connect(lambda: changed.append(True))

    root = panel.register_from_values("camera", "Camera archive", archive)

    assert root == ArchiveRoot(id="camera", name="Camera archive", path=archive)
    assert panel.last_error is None
    assert load_config(config_path).roots == [root]
    assert changed == [True]
    listed = panel.findChild(QListWidget)
    assert listed is not None and listed.count() == 1
    assert "camera" in listed.item(0).text()


def test_register_from_values_rejects_bad_input(config, archive, qapp):
    app_config, config_path = config
    panel = RootsPanel(app_config, config_path)

    assert panel.register_from_values("bad id", "Camera", archive) is None
    assert panel.last_error is not None
    assert panel.register_from_values("camera", "Camera", archive) is not None
    assert panel.register_from_values("camera", "Again", archive) is None
    assert "already registered" in panel.last_error
    assert len(app_config.roots) == 1


def test_register_from_values_rolls_back_when_the_write_fails(config, tmp_path, qapp):
    app_config, config_path = config
    config_path.mkdir()  # opening this path for writing fails with OSError
    panel = RootsPanel(app_config, config_path)
    root_dir = tmp_path / "archive"
    root_dir.mkdir()

    assert panel.register_from_values("camera", "Camera", root_dir) is None
    assert panel.last_error is not None
    assert app_config.roots == []


def test_root_dialog_prefills_and_shows_errors(archive, qapp):
    from PySide6.QtWidgets import QLabel

    dialog = RootDialog(archive)
    root_id, name, path = dialog.values()
    assert root_id.startswith("camera-")
    assert name == archive.name
    assert path == archive

    dialog.show_error("root id must be 1-64 letters")
    texts = [label.text() for label in dialog.findChildren(QLabel)]
    assert "root id must be 1-64 letters" in texts


def test_mapping_reminder_names_the_root_id(archive, qapp):
    root = ArchiveRoot(id="camera", name="Camera", path=archive)
    reminder = mapping_reminder(root)
    assert "camera" in reminder
    assert "map" in reminder


# -- removal guard (issue #82) -------------------------------------------------


def test_remove_button_is_enabled_only_with_a_selection(config, archive, qapp):
    app_config, config_path = config
    panel = RootsPanel(app_config, config_path)
    panel.register_from_values("camera", "Camera", archive)
    listed = panel.findChild(QListWidget)

    assert not panel.remove_button.isEnabled()
    listed.setCurrentRow(0)
    assert panel.remove_button.isEnabled()
    listed.setCurrentRow(-1)
    assert not panel.remove_button.isEnabled()


def test_remove_selected_refuses_with_non_terminal_clips(
    config, archive, tmp_path, qapp, monkeypatch
):
    app_config, config_path = config
    manifest_path = tmp_path / "manifest.sqlite"
    panel = RootsPanel(app_config, config_path, manifest_path=manifest_path)
    root = panel.register_from_values("camera", "Camera", archive)
    with Manifest.open(manifest_path) as manifest:
        manifest.upsert_clip(make_clip("camera", "A.MP4", JobState.QUEUED))
        manifest.upsert_clip(make_clip("camera", "B.MP4", JobState.ENCODING))
    spy = BoxSpy()
    monkeypatch.setattr(roots_panel, "QMessageBox", spy)
    panel.findChild(QListWidget).setCurrentRow(0)

    assert panel.remove_selected() is None

    assert app_config.roots == [root]
    assert load_config(config_path).roots == [root]
    assert spy.questions == []
    assert spy.warnings and "1 queued" in spy.warnings[0]
    assert "1 encoding" in spy.warnings[0]
    assert panel.last_error == spy.warnings[0]


def test_remove_selected_removes_and_keeps_manifest_history(
    config, archive, tmp_path, qapp, monkeypatch
):
    app_config, config_path = config
    manifest_path = tmp_path / "manifest.sqlite"
    panel = RootsPanel(app_config, config_path, manifest_path=manifest_path)
    root = panel.register_from_values("camera", "Camera", archive)
    with Manifest.open(manifest_path) as manifest:
        manifest.upsert_clip(make_clip("camera", "A.MP4", JobState.COMPLETE))
    spy = BoxSpy(answer=BoxSpy.StandardButton.Yes)
    monkeypatch.setattr(roots_panel, "QMessageBox", spy)
    changes: list[bool] = []
    panel.roots_changed.connect(lambda: changes.append(True))
    listed = panel.findChild(QListWidget)
    listed.setCurrentRow(0)

    assert panel.remove_selected() == root

    assert app_config.roots == []
    assert load_config(config_path).roots == []
    assert panel.last_error is None
    assert changes == [True]
    assert listed.count() == 0
    assert "Manifest history is kept" in spy.questions[0]
    assert "remote node configs are untouched" in spy.questions[0]

    with Manifest.open(manifest_path, create=False) as manifest:
        record = manifest.get_clip("camera", "A.MP4")
    assert record is not None and record.state is JobState.COMPLETE


def test_remove_selected_cancel_keeps_the_root(config, archive, qapp, monkeypatch):
    app_config, config_path = config
    panel = RootsPanel(app_config, config_path)
    root = panel.register_from_values("camera", "Camera", archive)
    spy = BoxSpy(answer=BoxSpy.StandardButton.No)
    monkeypatch.setattr(roots_panel, "QMessageBox", spy)
    panel.findChild(QListWidget).setCurrentRow(0)

    assert panel.remove_selected() is None

    assert app_config.roots == [root]
    assert panel.last_error is None
    assert spy.questions  # the operator was asked


def test_remove_selected_without_a_manifest_names_the_uncertainty(
    config, archive, qapp, monkeypatch
):
    app_config, config_path = config
    panel = RootsPanel(app_config, config_path)  # no manifest path: counts unknown
    root = panel.register_from_values("camera", "Camera", archive)
    spy = BoxSpy(answer=BoxSpy.StandardButton.Yes)
    monkeypatch.setattr(roots_panel, "QMessageBox", spy)
    panel.findChild(QListWidget).setCurrentRow(0)

    assert panel.remove_selected() == root

    assert app_config.roots == []
    assert "could not be checked" in spy.questions[0]
