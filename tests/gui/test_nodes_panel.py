"""Nodes panel and dialogs: config flows, background actions, states (issue #54)."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QCheckBox, QLabel

from gui.monitor_support import worker_payload, workers_payload
from gui.monitor_support_qt import dispose
from gui.nodes_support import (
    FakeBootstrap,
    FakeBootstrapFactory,
    FakeRegistryClient,
    make_entry,
)
from videocompressor.core.config import AppConfig, load_config, register_root
from videocompressor.core.nodes import NodeEntry
from videocompressor.gui.nodes_dialog import NodeDialog, RemoveNodeDialog, sanitize_node_id
from videocompressor.gui.nodes_panel import NodesPanel
from videocompressor.gui.nodes_runner import NodesRunner
from videocompressor.gui.nodes_service import NodesService
from videocompressor.node.models import SshError


def _spin_until(qapp, predicate: Callable[[], bool], timeout_s: float = 5.0) -> bool:
    """Process Qt events until ``predicate`` holds (QSignalSpy.wait is not reliable here)."""
    deadline = time.monotonic() + timeout_s
    while not predicate() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    qapp.processEvents()
    return predicate()


def _settle(qapp, panel: NodesPanel) -> None:
    """Wait for the current operation and deliver its queued result signal."""
    assert _spin_until(qapp, lambda: not panel.runner.busy)
    qapp.processEvents()


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    root = tmp_path / "archive"
    root.mkdir()
    return root


@pytest.fixture
def config(archive: Path) -> AppConfig:
    app_config = AppConfig()
    register_root(app_config, root_id="archive", name="Archive", path=archive)
    return app_config


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    return tmp_path / "config.toml"


@pytest.fixture
def panel_factory(qapp, config: AppConfig, config_path: Path):
    created: list[NodesPanel] = []

    def make(*, bootstrap: FakeBootstrap | None = None, client: object | None = None):
        factory = FakeBootstrapFactory(bootstrap if bootstrap is not None else FakeBootstrap())
        service = NodesService(config, bootstrap_factory=factory, client=client)
        panel = NodesPanel(
            config=config,
            config_path=config_path,
            service=service,
            runner=NodesRunner(pool=QThreadPool()),
        )
        created.append(panel)
        _settle(qapp, panel)
        return panel, factory

    yield make
    for panel in created:
        panel.shutdown()
        dispose(panel)


# -- table/state ------------------------------------------------------------


def test_panel_lists_configured_nodes(panel_factory, config):
    config.nodes.append(make_entry(id="node-a"))
    config.nodes.append(
        make_entry(
            id="node-b",
            name="",
            host="node.example.com",
            user="",
            port=None,
            roots={"archive": "/srv/archive"},
        )
    )
    panel, _factory = panel_factory()

    model = panel.model
    assert model.rowCount() == 2
    assert model.index(0, 0).data() == "Test node A (node-a)"
    assert model.index(0, 1).data() == "example@192.0.2.10:22"
    assert model.index(0, 2).data() == "archive → /home/example/archive"
    assert model.index(0, 3).data() == "not probed yet"
    assert model.index(0, 4).data() == "unknown"
    assert model.index(1, 0).data() == "node-b"
    assert model.index(1, 1).data() == "node.example.com"


def test_registry_column_merges_online_ids(qapp, panel_factory, config):
    client = FakeRegistryClient(workers_payload(worker_payload("node-a", online=True)))
    config.nodes.append(make_entry())
    panel, _factory = panel_factory(client=client)

    assert panel.model.index(0, 4).data() == "online"

    client.error = RuntimeError("controller down")
    panel.refresh_registry()
    _settle(qapp, panel)
    assert panel.model.index(0, 4).data() == "unknown"


def test_actions_disable_while_an_operation_runs(qapp, panel_factory, config):
    config.nodes.append(make_entry())
    panel, _factory = panel_factory()
    panel.select_node("node-a")
    assert panel.probe_button.isEnabled()

    release = threading.Event()
    assert panel.runner.submit("slow", lambda: release.wait(2)) is True
    assert not panel.add_button.isEnabled()
    assert not panel.probe_button.isEnabled()
    assert not panel.remove_button.isEnabled()

    release.set()
    _settle(qapp, panel)
    assert panel.probe_button.isEnabled()


def test_probe_without_a_selection_shows_a_hint(panel_factory, config):
    panel, _factory = panel_factory()
    panel.probe_selected()
    assert panel.status_label.text() == "select a node first"


# -- config flows -----------------------------------------------------------


def test_add_from_values_persists_lists_and_probes(qapp, panel_factory, config, config_path):
    panel, factory = panel_factory()
    added = panel.add_from_values(make_entry())

    assert added is not None
    assert panel.last_error is None
    assert load_config(config_path).nodes == [make_entry()]
    assert factory.entries == [make_entry()]

    _settle(qapp, panel)
    assert panel.model.index(0, 3).data().startswith("probe ok:")
    assert "added node node-a" in panel.activity_text()


def test_add_from_values_rejects_duplicate_ids(panel_factory, config, config_path):
    config.nodes.append(make_entry())
    panel, _factory = panel_factory()

    assert panel.add_from_values(make_entry(name="Duplicate")) is None
    assert "already registered" in (panel.last_error or "")
    assert config.nodes == [make_entry()]
    assert not config_path.exists()


def test_edit_from_values_updates_and_persists(panel_factory, config, config_path):
    config.nodes.append(make_entry())
    panel, _factory = panel_factory()

    updated = panel.edit_from_values("node-a", make_entry(name="Renamed", thread_cap=4))

    assert updated is not None
    loaded = load_config(config_path)
    assert loaded.nodes[0].name == "Renamed"
    assert loaded.nodes[0].thread_cap == 4
    assert panel.edit_from_values("ghost", make_entry(id="ghost")) is None
    assert "unknown node" in (panel.last_error or "")


def test_remove_node_uninstalls_then_drops_the_entry(qapp, panel_factory, config, config_path):
    bootstrap = FakeBootstrap()
    config.nodes.append(make_entry())
    panel, _factory = panel_factory(bootstrap=bootstrap)

    assert panel.remove_node("node-a", purge=True) is True
    _settle(qapp, panel)

    assert bootstrap.purges == [True]
    assert config.nodes == []
    assert load_config(config_path).nodes == []
    assert "uninstall ok" in panel.activity_text()


def test_remove_node_keeps_the_entry_when_uninstall_fails(qapp, panel_factory, config):
    bootstrap = FakeBootstrap(error=SshError("ssh failed: connection refused"))
    config.nodes.append(make_entry())
    panel, _factory = panel_factory(bootstrap=bootstrap)

    assert panel.remove_node("node-a") is True
    _settle(qapp, panel)

    assert config.nodes == [make_entry()]
    assert "uninstall failed" in panel.activity_text()
    assert "ssh failed" in panel.activity_text()


def test_remove_without_uninstall_drops_the_entry(panel_factory, config, config_path):
    config.nodes.append(make_entry())
    panel, _factory = panel_factory()

    assert panel.remove_node("node-a", uninstall=False) is True
    assert config.nodes == []
    assert load_config(config_path).nodes == []


def test_remove_unknown_node_reports_an_error(panel_factory, config):
    panel, _factory = panel_factory()
    assert panel.remove_node("ghost") is False
    assert "unknown node" in (panel.last_error or "")


# -- dialogs ----------------------------------------------------------------


def test_node_dialog_round_trips_the_entry(qapp):
    dialog = NodeDialog(["archive", "compressed"], entry=make_entry())
    assert dialog.values() == make_entry()


def test_node_dialog_preserves_unregistered_root_mappings(qapp):
    dialog = NodeDialog(["archive"], entry=make_entry())
    dialog.set_values(
        make_entry(roots={"archive": "/srv/archive", "legacy": "/srv/legacy"})
    )
    assert dialog.values().roots == {"archive": "/srv/archive", "legacy": "/srv/legacy"}


def test_node_dialog_prefills_the_id_from_the_host(qapp):
    dialog = NodeDialog(["archive"])
    dialog.set_values(NodeEntry(id="", host="node.example.com"))
    dialog.prefill_id_from_host()
    assert dialog.values().id == "node.example.com"

    dialog.set_values(NodeEntry(id="node-a", host="other.example.com"))
    dialog.prefill_id_from_host()
    assert dialog.values().id == "node-a"


def test_node_dialog_shows_errors_inline(qapp):
    dialog = NodeDialog(["archive"])
    dialog.show_error("host is required")
    texts = [label.text() for label in dialog.findChildren(QLabel)]
    assert "host is required" in texts


def test_sanitize_node_id():
    assert sanitize_node_id("node.example.com") == "node.example.com"
    assert sanitize_node_id("192.0.2.10") == "192.0.2.10"
    assert sanitize_node_id("bad host!") == "bad-host-"
    assert sanitize_node_id("   ") == ""


def test_remove_dialog_purge_requires_uninstall(qapp):
    dialog = RemoveNodeDialog(make_entry())
    assert dialog.uninstall() is True
    assert dialog.purge() is False

    dialog.findChild(QCheckBox, "purge").setChecked(True)
    assert dialog.purge() is True

    dialog.findChild(QCheckBox, "uninstall-service").setChecked(False)
    assert dialog.uninstall() is False
    assert dialog.purge() is False
