"""Add/edit/remove dialogs for the Nodes tab (issue #54)."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from videocompressor.core.nodes import NODE_ID_PATTERN, NodeEntry

__all__ = ["NodeDialog", "RemoveNodeDialog", "sanitize_node_id"]

_ROOT_HINT = (
    "Each row maps a controller root id to the path on that host. The worker "
    "receives these as its own [roots] mapping; paths may start with ~."
)


def sanitize_node_id(host: str) -> str:
    """Derive a worker id from a host name; empty when nothing usable remains."""
    candidate = re.sub(r"[^A-Za-z0-9_.-]", "-", host.strip())[:64]
    return candidate if candidate and NODE_ID_PATTERN.fullmatch(candidate) else ""


class NodeDialog(QDialog):
    """Collect one worker node with inline validation errors."""

    def __init__(
        self,
        root_ids: Sequence[str],
        *,
        entry: NodeEntry | None = None,
        default_controller_url: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._root_ids = list(root_ids)
        self.setWindowTitle("Edit node" if entry is not None else "Add node")

        self._id = QLineEdit()
        self._id.setPlaceholderText("node-a")
        self._name = QLineEdit()
        self._name.setPlaceholderText("display name (optional)")
        self._host = QLineEdit()
        self._host.setPlaceholderText("host or IP")
        self._host.editingFinished.connect(self.prefill_id_from_host)
        self._user = QLineEdit()
        self._user.setPlaceholderText("ssh user (optional)")
        self._port = QSpinBox()
        self._port.setRange(0, 65535)
        self._port.setSpecialValueText("ssh default")
        self._identity = QLineEdit()
        self._identity.setPlaceholderText("private key file (optional)")
        identity_browse = QPushButton("Browse…")
        identity_browse.clicked.connect(self._browse_identity)
        identity_row = QHBoxLayout()
        identity_row.addWidget(self._identity)
        identity_row.addWidget(identity_browse)
        self._strict = QCheckBox("Require a known host key (strict)")
        self._controller_url = QLineEdit()
        self._controller_url.setPlaceholderText("http://<address this node can reach>:8765")
        controller_hint = QLabel(
            "The node must reach this address; use the Mac's LAN or Tailscale address,"
            " never its loopback."
        )
        controller_hint.setWordWrap(True)
        self._scratch = QLineEdit()
        self._scratch.setPlaceholderText("~/scratch")
        self._thread_cap = QSpinBox()
        self._thread_cap.setRange(1, 64)
        self._install_dir = QLineEdit()
        self._install_dir.setPlaceholderText("~/.videocompressor")

        self._roots = QTableWidget()
        self._roots.setColumnCount(2)
        self._roots.setHorizontalHeaderLabels(("root id", "path on that host"))
        self._roots.verticalHeader().hide()
        self._roots.horizontalHeader().setStretchLastSection(True)
        self._roots.setMinimumHeight(90)
        root_hint = QLabel(_ROOT_HINT)
        root_hint.setWordWrap(True)

        self._error = QLabel("")
        self._error.setWordWrap(True)

        form = QFormLayout(self)
        form.addRow("Node id", self._id)
        form.addRow("Name", self._name)
        form.addRow("Host / IP", self._host)
        form.addRow("SSH user", self._user)
        form.addRow("SSH port", self._port)
        form.addRow("Identity", identity_row)
        form.addRow(self._strict)
        form.addRow("Controller URL", self._controller_url)
        form.addRow(controller_hint)
        form.addRow("Scratch dir", self._scratch)
        form.addRow("Thread cap", self._thread_cap)
        form.addRow("Install dir", self._install_dir)
        form.addRow("Root mappings", self._roots)
        form.addRow(root_hint)
        form.addRow(self._error)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

        self.set_values(entry if entry is not None else NodeEntry(id=""))
        if entry is None and default_controller_url:
            self._controller_url.setText(default_controller_url)
        if not self._root_ids:
            root_hint.setText(
                "No archive roots are registered yet; register one on the Library tab first."
            )

    # -- data ---------------------------------------------------------------

    def values(self) -> NodeEntry:
        """The typed values as a :class:`NodeEntry` (empty root paths are dropped)."""
        roots: dict[str, str] = {}
        for row in range(self._roots.rowCount()):
            root_item = self._roots.item(row, 0)
            path_item = self._roots.item(row, 1)
            if root_item is None or path_item is None:
                continue
            path = path_item.text().strip()
            if path:
                roots[root_item.text().strip()] = path
        return NodeEntry(
            id=self._id.text().strip(),
            name=self._name.text().strip(),
            host=self._host.text().strip(),
            user=self._user.text().strip(),
            port=self._port.value() or None,
            identity=self._identity.text().strip(),
            strict_host_keys=self._strict.isChecked(),
            controller_url=self._controller_url.text().strip(),
            roots=roots,
            scratch_dir=self._scratch.text().strip(),
            thread_cap=self._thread_cap.value(),
            install_dir=self._install_dir.text().strip(),
        )

    def set_values(self, entry: NodeEntry) -> None:
        """Load ``entry`` into the fields (also used by the edit flow and tests)."""
        self._id.setText(entry.id)
        self._name.setText(entry.name)
        self._host.setText(entry.host)
        self._user.setText(entry.user)
        self._port.setValue(entry.port or 0)
        self._identity.setText(entry.identity)
        self._strict.setChecked(entry.strict_host_keys)
        self._controller_url.setText(entry.controller_url)
        self._scratch.setText(entry.scratch_dir)
        self._thread_cap.setValue(entry.thread_cap)
        self._install_dir.setText(entry.install_dir)
        self._fill_roots(entry.roots)

    def show_error(self, message: str) -> None:
        """Show a validation error without closing the dialog."""
        self._error.setText(message)

    def prefill_id_from_host(self) -> None:
        """Derive the node id from the host when the id is still empty."""
        if self._id.text().strip():
            return
        derived = sanitize_node_id(self._host.text())
        if derived:
            self._id.setText(derived)

    # -- internals ----------------------------------------------------------

    def _fill_roots(self, roots: Mapping[str, str]) -> None:
        paths = dict(roots)
        root_ids = list(self._root_ids)
        root_ids += [root_id for root_id in paths if root_id not in root_ids]
        self._roots.setRowCount(len(root_ids))
        for row, root_id in enumerate(root_ids):
            id_item = QTableWidgetItem(root_id)
            id_item.setFlags(id_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            path_item = QTableWidgetItem(paths.get(root_id, ""))
            self._roots.setItem(row, 0, id_item)
            self._roots.setItem(row, 1, path_item)

    def _browse_identity(self) -> None:
        chosen, _filter = QFileDialog.getOpenFileName(
            self, "Choose SSH private key", self._identity.text().strip()
        )
        if chosen:
            self._identity.setText(chosen)


class RemoveNodeDialog(QDialog):
    """Confirm removing a node and choose the remote uninstall options."""

    def __init__(self, node: NodeEntry, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Remove node")
        title = QLabel(f"Remove node {node.display_name!r} ({node.host}) from the app?")
        title.setWordWrap(True)
        detail = QLabel(
            "When the host is reachable, its worker service is stopped and the pushed "
            "worker.toml is removed first; scratch content and the archive are never touched."
        )
        detail.setWordWrap(True)
        self._uninstall = QCheckBox("Stop and remove the worker service on the host")
        self._uninstall.setChecked(True)
        self._uninstall.setObjectName("uninstall-service")
        self._purge = QCheckBox(
            "Also remove the managed install directory on the host (purge)"
        )
        self._purge.setObjectName("purge")

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(detail)
        layout.addWidget(self._uninstall)
        layout.addWidget(self._purge)
        layout.addWidget(buttons)

    def uninstall(self) -> bool:
        """Whether the worker service is stopped and removed first."""
        return self._uninstall.isChecked()

    def purge(self) -> bool:
        """Whether the managed install directory is removed as well."""
        return self._purge.isChecked() and self._uninstall.isChecked()
