"""Root registration: validation, persistence, and the mapping reminder (issue #8)."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QListWidget

from videocompressor.core.config import AppConfig, load_config
from videocompressor.core.models import ArchiveRoot
from videocompressor.gui.roots_panel import RootDialog, RootsPanel, mapping_reminder


@pytest.fixture
def config(tmp_path):
    return AppConfig(), tmp_path / "config.toml"


@pytest.fixture
def archive(tmp_path):
    root = tmp_path / "share" / "CAMERA"
    root.mkdir(parents=True)
    return root


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
