"""Convert-flow root auto-registration and the enqueue notes (issue #82).

These replace the old dead-end tests (a drop outside the roots registering a
problem row and disabling Start); the behavior is now "register and resolve".
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6")

import shiboken6
from PySide6.QtWidgets import QWidget

from videocompressor.core.config import AppConfig, load_config, register_root
from videocompressor.core.nodes import NodeEntry
from videocompressor.gui import convert_panel


class FakeEnqueueClient:
    """Records payloads and returns a canned response (or raises)."""

    def __init__(self, response: dict | None = None, error: Exception | None = None) -> None:
        self.payloads: list[dict] = []
        self.response = (
            response
            if response is not None
            else {"jobs": [], "queued": 0, "skipped": 0, "rejected": 0}
        )
        self.error = error

    def enqueue(self, payload) -> dict:
        self.payloads.append(payload)
        if self.error is not None:
            raise self.error
        return self.response


def clip(path: Path, payload: bytes = b"x" * 64) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def queued_response(root_id: str, relative_path: str) -> dict:
    return {
        "jobs": [
            {
                "root_id": root_id,
                "relative_path": relative_path,
                "status": "queued",
                "reason": None,
            }
        ],
        "queued": 1,
        "skipped": 0,
        "rejected": 0,
    }


@pytest.fixture
def panel_factory(qapp, tmp_path):
    """Build panels with a real config path in ``tmp_path``; delete after the test."""
    created: list[QWidget] = []

    def build(*, client=None, config=None, config_path=None):
        config = config if config is not None else AppConfig()
        config_path = (
            config_path if config_path is not None else tmp_path / "config.toml"
        )
        client = client if client is not None else FakeEnqueueClient()
        panel = convert_panel.ConvertPanel(client, config, config_path)
        created.append(panel)
        return panel, client, config

    yield build

    for widget in created:
        if shiboken6.isValid(widget):
            shiboken6.delete(widget)


# -- input auto-registration --------------------------------------------------


def test_drop_outside_roots_registers_a_root_and_resolves(qapp, tmp_path, panel_factory):
    panel, _client, config = panel_factory()
    folder = tmp_path / "ride"
    clip(folder / "GS010001.MP4")
    clip(folder / "notes.txt")
    changes: list[bool] = []
    panel.roots_changed.connect(lambda: changes.append(True))

    panel.drop_zone.paths_dropped.emit([folder])

    (root,) = config.roots
    assert root.name == "ride"
    assert root.path == folder.resolve()
    assert load_config(tmp_path / "config.toml").roots == [root]
    assert panel.pending_table.rowCount() == 1
    assert panel.pending_table.item(0, 2).text() == "ready"
    assert "registered new root" in panel.message_label.text()
    assert root.id in panel.message_label.text()
    assert panel.start_button.isEnabled()
    assert changes == [True]


def test_drop_under_a_root_adds_no_root_and_keeps_the_mapping(qapp, tmp_path, panel_factory):
    config = AppConfig()
    archive = tmp_path / "archive"
    archive.mkdir()
    register_root(config, root_id="archive", name="Archive", path=archive)
    panel, _client, _config = panel_factory(config=config)

    panel.add_paths([clip(archive / "A.MP4")])

    assert [root.id for root in config.roots] == ["archive"]
    (row,) = panel.selection.entries()
    assert (row.root_id, row.relative_path) == ("archive", "A.MP4")
    assert panel.start_button.isEnabled()


def test_drop_of_a_folder_inside_a_root_resolves_to_it(qapp, tmp_path, panel_factory):
    config = AppConfig()
    archive = tmp_path / "archive"
    folder = archive / "2026-06-Ride"
    clip(folder / "GS010001.MP4")
    register_root(config, root_id="archive", name="Archive", path=archive)
    panel, _client, _config = panel_factory(config=config)

    panel.add_paths([folder])

    assert [root.id for root in config.roots] == ["archive"]
    (row,) = panel.selection.entries()
    assert row.root_id == "archive"
    assert panel.start_button.isEnabled()


def test_registration_write_failure_leaves_problem_rows(qapp, tmp_path, panel_factory):
    config_path = tmp_path / "config.toml"
    config_path.mkdir()  # opening this path for writing fails with OSError
    panel, _client, config = panel_factory(config_path=config_path)
    folder = tmp_path / "ride"
    clip(folder / "GS010001.MP4")

    panel.add_paths([folder])

    assert config.roots == []
    (row,) = panel.selection.entries()
    assert not row.ready
    assert row.problem is not None and "registered root" in row.problem
    assert "cannot save" in panel.message_label.text()
    assert not panel.start_button.isEnabled()


# -- output auto-registration -------------------------------------------------


def test_custom_folder_outside_roots_registers_on_start(qapp, tmp_path, panel_factory):
    config = AppConfig()
    archive = tmp_path / "archive"
    archive.mkdir()
    register_root(config, root_id="archive", name="Archive", path=archive)
    panel, client, _config = panel_factory(config=config)
    panel.add_paths([clip(archive / "A.MP4")])
    panel.mode_combo.setCurrentIndex(1)  # Custom folder
    outside = tmp_path / "out"
    outside.mkdir()
    panel.folder_edit.setText(str(outside))
    changes: list[bool] = []
    panel.roots_changed.connect(lambda: changes.append(True))

    assert panel.start_button.isEnabled()
    client.response = queued_response("archive", "A.MP4")

    panel.start()

    (out_root,) = [root for root in config.roots if root.path == outside.resolve()]
    (payload,) = client.payloads
    assert payload["destination"] == {"kind": "output_root", "root_id": out_root.id}
    assert payload["files"] == [{"root_id": "archive", "relative_path": "A.MP4"}]
    assert load_config(tmp_path / "config.toml").roots == config.roots
    assert out_root.id in panel.message_label.text()
    assert "as an output root" in panel.message_label.text()
    assert changes == [True]


def test_custom_folder_that_is_a_root_mirrors_without_registration(
    qapp, tmp_path, panel_factory
):
    config = AppConfig()
    archive = tmp_path / "archive"
    archive.mkdir()
    register_root(config, root_id="archive", name="Archive", path=archive)
    panel, client, _config = panel_factory(config=config)
    panel.add_paths([clip(archive / "A.MP4")])
    panel.mode_combo.setCurrentIndex(1)  # Custom folder
    panel.folder_edit.setText(str(archive))
    client.response = queued_response("archive", "A.MP4")

    panel.start()

    assert [root.id for root in config.roots] == ["archive"]
    (payload,) = client.payloads
    assert payload["destination"] == {"kind": "output_root", "root_id": "archive"}


# -- remote-node visibility ---------------------------------------------------


def test_start_warns_about_nodes_missing_a_root_used_by_the_batch(
    qapp, tmp_path, panel_factory
):
    config = AppConfig()
    config.nodes = [
        NodeEntry(id="homeserver", name="Linux node", roots={"archive": "/srv/archive"})
    ]
    panel, client, _config = panel_factory(config=config)
    folder = tmp_path / "ride"
    clip(folder / "A.MP4")
    panel.add_paths([folder])
    (root,) = config.roots
    client.response = queued_response(root.id, "A.MP4")

    panel.start()

    message = panel.message_label.text()
    assert "Linux node" in message
    assert root.id in message
    assert "vc node install" in message


def test_start_does_not_warn_when_the_node_maps_the_root(qapp, tmp_path, panel_factory):
    config = AppConfig()
    panel, client, _config = panel_factory(config=config)
    folder = tmp_path / "ride"
    clip(folder / "A.MP4")
    panel.add_paths([folder])
    (root,) = config.roots
    config.nodes = [
        NodeEntry(id="homeserver", name="Linux node", roots={root.id: "/srv/ride"})
    ]
    client.response = queued_response(root.id, "A.MP4")

    panel.start()

    assert "vc node install" not in panel.message_label.text()
