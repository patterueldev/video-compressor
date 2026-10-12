"""Nodes tab: add, configure, probe, install, and remove worker nodes (issue #54).

The panel is a thin Qt layer over :class:`NodesService` (config entries to #53
bootstrap calls) and :class:`NodesRunner` (one background operation at a time).
Add/edit/remove persist through ``core.nodes`` and the per-user config; the
worker's own ``worker.toml`` is rendered and pushed only by the backend. The
table merges the configured entries with the latest activity and the controller's
online ids.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any

from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from videocompressor.core.config import AppConfig, save_config
from videocompressor.core.nodes import (
    NodeEntry,
)
from videocompressor.core.nodes import (
    add_node as config_add_node,
)
from videocompressor.core.nodes import (
    update_node as config_update_node,
)
from videocompressor.gui.nodes_dialog import NodeDialog, RemoveNodeDialog
from videocompressor.gui.nodes_runner import NodesRunner
from videocompressor.gui.nodes_service import NodeActivity, NodesService, default_controller_url
from videocompressor.gui.nodes_tables import NodesConfigModel

__all__ = ["NodesPanel"]


class NodesPanel(QWidget):
    """Manage the configured worker nodes and their per-node root mappings."""

    def __init__(
        self,
        *,
        config: AppConfig,
        config_path: Path,
        client: Any | None = None,
        service: NodesService | None = None,
        runner: NodesRunner | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.config = config
        self.config_path = config_path
        self.last_error: str | None = None
        self.service = service if service is not None else NodesService(config, client=client)
        self._pending_removal: NodeEntry | None = None

        self.model = NodesConfigModel(self)
        self.view = _table_view(self.model)
        self.add_button = QPushButton("Add node…")
        self.edit_button = QPushButton("Edit…")
        self.remove_button = QPushButton("Remove…")
        self.probe_button = QPushButton("Probe")
        self.install_button = QPushButton("Install / update")
        self.registry_button = QPushButton("Refresh")
        self.status_label = QLabel("ready")
        self.activity_log = QPlainTextEdit()
        self.activity_log.setReadOnly(True)
        self.activity_log.setMaximumBlockCount(1000)

        self.runner = runner if runner is not None else NodesRunner(parent=self)
        self.runner.started.connect(self._on_started)
        self.runner.finished.connect(self._on_finished)
        self.runner.failed.connect(self._on_failed)
        self.runner.start()

        self._build_layout()
        self.view.selectionModel().selectionChanged.connect(self._update_buttons)
        self.add_button.clicked.connect(self.prompt_add)
        self.edit_button.clicked.connect(self.prompt_edit)
        self.remove_button.clicked.connect(self.prompt_remove)
        self.probe_button.clicked.connect(self.probe_selected)
        self.install_button.clicked.connect(self.install_selected)
        self.registry_button.clicked.connect(self.refresh_registry)
        self.refresh_list()
        self.refresh_registry()

    # -- reading -----------------------------------------------------------

    def selected_node_id(self) -> str | None:
        """The node id of the selected table row, if any."""
        indexes = self.view.selectionModel().selectedRows()
        if not indexes:
            return None
        row = indexes[0].row()
        if row < 0 or row >= self.model.rowCount():
            return None
        return self.model.node_at(row).id

    def select_node(self, node_id: str) -> None:
        """Select the row for ``node_id`` when it is displayed."""
        row = self.model.row_of(node_id)
        if row is not None:
            self.view.selectRow(row)

    def activity_text(self) -> str:
        """The activity log's current text (for tests)."""
        return self.activity_log.toPlainText()

    def refresh_list(self) -> None:
        """Rebuild the table from the current config, keeping the selection."""
        selected = self.selected_node_id()
        self.model.set_nodes(self.config.nodes)
        if selected is not None:
            self.select_node(selected)
        self._update_buttons()

    def refresh_registry(self) -> None:
        """Ask the controller which node ids are online (background, fail-soft)."""
        self._submit("refresh controller registry", self.service.registry_online)

    # -- config actions ----------------------------------------------------

    def add_from_values(self, entry: NodeEntry) -> NodeEntry | None:
        """Validate, persist, list, and probe one node; ``None`` with ``last_error``."""
        count = len(self.config.nodes)
        try:
            added = config_add_node(self.config.nodes, entry)
            save_config(self.config, self.config_path)
        except (ValueError, OSError) as error:
            del self.config.nodes[count:]
            self.last_error = str(error)
            return None
        self.last_error = None
        self.refresh_list()
        self.select_node(added.id)
        self._append_line(f"added node {added.id}")
        self.probe_selected()
        return added

    def edit_from_values(self, node_id: str, entry: NodeEntry) -> NodeEntry | None:
        """Validate and persist an edited node; ``None`` with ``last_error``."""
        index = next((i for i, node in enumerate(self.config.nodes) if node.id == node_id), None)
        if index is None:
            self.last_error = f"unknown node {node_id!r}"
            return None
        previous = self.config.nodes[index]
        try:
            updated = config_update_node(self.config.nodes, node_id, entry)
            save_config(self.config, self.config_path)
        except (ValueError, OSError) as error:
            self.config.nodes[index] = previous
            self.last_error = str(error)
            return None
        self.last_error = None
        self.refresh_list()
        self.select_node(updated.id)
        self._append_line(f"updated node {updated.id}")
        return updated

    def remove_node(self, node_id: str, *, purge: bool = False, uninstall: bool = True) -> bool:
        """Remove a node; ``uninstall`` stops its worker service first when reachable.

        With ``uninstall`` the config entry is dropped only after the uninstall
        succeeds, so an unreachable host keeps its entry (retry, or remove with
        ``uninstall=False``). Returns whether the removal was started/settled.
        """
        entry = next((node for node in self.config.nodes if node.id == node_id), None)
        if entry is None:
            self.last_error = f"unknown node {node_id!r}"
            return False
        if not uninstall:
            return self._drop_entry(node_id)
        if self.runner.busy:
            self.last_error = "another node operation is running; wait for it to finish"
            return False
        self._pending_removal = entry
        operation = partial(self.service.uninstall, entry, purge=purge)
        if not self.runner.submit(f"remove {entry.id}", operation):
            self._pending_removal = None
            self.last_error = "another node operation is running; wait for it to finish"
            return False
        self.last_error = None
        return True

    # -- prompts -----------------------------------------------------------

    def prompt_add(self) -> NodeEntry | None:
        """Modal add loop; returns the new node or ``None`` when cancelled."""
        dialog = NodeDialog(
            self._root_ids(),
            default_controller_url=default_controller_url(self.config),
            parent=self,
        )
        while dialog.exec() == QDialog.DialogCode.Accepted:
            added = self.add_from_values(dialog.values())
            if added is not None:
                return added
            dialog.show_error(self.last_error or "registration failed")
        return None

    def prompt_edit(self) -> NodeEntry | None:
        """Modal edit loop; returns the updated node or ``None`` when cancelled."""
        entry = self._selected_entry()
        if entry is None:
            self.status_label.setText("select a node first")
            return None
        dialog = NodeDialog(self._root_ids(), entry=entry, parent=self)
        while dialog.exec() == QDialog.DialogCode.Accepted:
            updated = self.edit_from_values(entry.id, dialog.values())
            if updated is not None:
                return updated
            dialog.show_error(self.last_error or "update failed")
        return None

    def prompt_remove(self) -> bool | None:
        """Confirm removal of the selected node; ``None`` when cancelled."""
        entry = self._selected_entry()
        if entry is None:
            self.status_label.setText("select a node first")
            return None
        dialog = RemoveNodeDialog(entry, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return self.remove_node(entry.id, purge=dialog.purge(), uninstall=dialog.uninstall())

    # -- node actions ------------------------------------------------------

    def probe_selected(self) -> None:
        """Run the read-only probe for the selected node in the background."""
        entry = self._selected_entry()
        if entry is None:
            self.status_label.setText("select a node first")
            return
        self._submit(f"probe {entry.id}", partial(self.service.probe, entry))

    def install_selected(self) -> None:
        """Install or update the worker on the selected node in the background."""
        entry = self._selected_entry()
        if entry is None:
            self.status_label.setText("select a node first")
            return
        self._submit(f"install {entry.id}", partial(self.service.install, entry))

    def shutdown(self) -> None:
        """Stop accepting results; an in-flight SSH operation finishes unnoticed."""
        self.runner.stop()

    # -- internals ---------------------------------------------------------

    def _build_layout(self) -> None:
        header = QHBoxLayout()
        header.addWidget(QLabel("Worker nodes"), 1)
        header.addWidget(self.registry_button)

        actions = QHBoxLayout()
        actions.addWidget(self.add_button)
        actions.addWidget(self.edit_button)
        actions.addWidget(self.remove_button)
        actions.addStretch(1)
        actions.addWidget(self.probe_button)
        actions.addWidget(self.install_button)

        logs = QVBoxLayout()
        logs.addWidget(QLabel("Activity"))
        logs.addWidget(self.activity_log, 1)

        layout = QVBoxLayout(self)
        layout.addLayout(header)
        layout.addWidget(self.view, 1)
        layout.addLayout(actions)
        layout.addLayout(logs, 1)
        layout.addWidget(self.status_label)

    def _root_ids(self) -> list[str]:
        return [root.id for root in self.config.roots]

    def _selected_entry(self) -> NodeEntry | None:
        node_id = self.selected_node_id()
        if node_id is None:
            return None
        return next((node for node in self.config.nodes if node.id == node_id), None)

    def _submit(self, label: str, operation: Callable[[], object]) -> None:
        if not self.runner.submit(label, operation):
            self.status_label.setText("an operation is already running; wait for it to finish")

    def _drop_entry(self, node_id: str) -> bool:
        index = next((i for i, node in enumerate(self.config.nodes) if node.id == node_id), None)
        if index is None:
            self.last_error = f"unknown node {node_id!r}"
            return False
        removed = self.config.nodes.pop(index)
        try:
            save_config(self.config, self.config_path)
        except OSError as error:
            self.config.nodes.insert(index, removed)
            self.last_error = str(error)
            return False
        self.last_error = None
        self.refresh_list()
        self._append_line(f"removed node {node_id}")
        return True

    def _on_started(self, label: str) -> None:
        self.status_label.setText(f"{label}…")
        self._update_buttons()

    def _on_finished(self, result: object) -> None:
        if isinstance(result, NodeActivity):
            self.model.set_activity(result.node_id, result.summary)
            self._append_activity(result)
            self.status_label.setText(result.summary)
            if self._pending_removal is not None and result.operation == "uninstall":
                pending, self._pending_removal = self._pending_removal, None
                if result.ok:
                    self._drop_entry(pending.id)
        elif result is None or isinstance(result, set):
            self.model.set_online(result)  # type: ignore[arg-type]
            if result is None:
                self.status_label.setText("controller unreachable; online states unknown")
            else:
                self.status_label.setText(f"controller: {len(result)} node(s) online")
        self._update_buttons()

    def _on_failed(self, message: str) -> None:
        self.status_label.setText(message)
        self._append_line(f"error: {message}")
        self._pending_removal = None
        self._update_buttons()

    def _append_activity(self, activity: NodeActivity) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self._append_line(f"{stamp} {activity.summary}")
        for line in activity.lines:
            self._append_line(f"    {line}")

    def _append_line(self, line: str) -> None:
        self.activity_log.appendPlainText(line)

    def _update_buttons(self) -> None:
        busy = self.runner.busy
        has_selection = self._selected_entry() is not None
        self.add_button.setEnabled(not busy)
        self.registry_button.setEnabled(not busy)
        self.edit_button.setEnabled(not busy and has_selection)
        self.remove_button.setEnabled(not busy and has_selection)
        self.probe_button.setEnabled(not busy and has_selection)
        self.install_button.setEnabled(not busy and has_selection)


def _table_view(model: NodesConfigModel) -> QTableView:
    view = QTableView()
    view.setModel(model)
    view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    view.verticalHeader().setVisible(False)
    view.horizontalHeader().setStretchLastSection(True)
    view.setSortingEnabled(False)
    view.setAlternatingRowColors(True)
    return view
