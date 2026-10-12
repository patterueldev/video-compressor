"""Read-only Nodes-tab table (issue #54).

``NodesConfigModel`` renders one row per configured node: the SSH target, the
root mappings, the latest activity summary, and the controller's online state
merged from ``GET /api/v1/workers`` (``None`` when that fetch is unavailable).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, Qt

from videocompressor.core.nodes import NodeEntry

__all__ = ["NODE_CONFIG_COLUMNS", "NodesConfigModel"]

NODE_CONFIG_COLUMNS = ("node", "host", "roots", "last activity", "controller")

NOT_PROBED = "not probed yet"
UNKNOWN = "unknown"
OFFLINE = "offline"
ONLINE = "online"


class NodesConfigModel(QAbstractTableModel):
    """One row per configured node, merged with the session's activity and registry."""

    columns = NODE_CONFIG_COLUMNS

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._nodes: tuple[NodeEntry, ...] = ()
        self._activity: dict[str, str] = {}
        self._online: set[str] | None = None

    # -- data ---------------------------------------------------------------

    def set_nodes(self, nodes: Sequence[NodeEntry]) -> None:
        """Replace the displayed rows with ``nodes``."""
        self.beginResetModel()
        self._nodes = tuple(nodes)
        self.endResetModel()

    def set_activity(self, node_id: str, summary: str) -> None:
        """Record the latest probe/install/uninstall summary for one node."""
        row = self.row_of(node_id)
        if row is None:
            return
        self._activity[node_id] = summary
        index = self.index(row, 3)
        self.dataChanged.emit(index, index)

    def set_online(self, online: set[str] | None) -> None:
        """Record the controller's online ids (``None`` = unavailable)."""
        self._online = online
        if self._nodes:
            self.dataChanged.emit(self.index(0, 4), self.index(len(self._nodes) - 1, 4))

    def clear_activity(self) -> None:
        """Forget every recorded activity summary."""
        self._activity.clear()
        if self._nodes:
            self.dataChanged.emit(self.index(0, 3), self.index(len(self._nodes) - 1, 3))

    def node_at(self, row: int) -> NodeEntry:
        """The node backing the displayed row."""
        return self._nodes[row]

    def row_of(self, node_id: str) -> int | None:
        """The displayed row for ``node_id``, or ``None``."""
        for row, node in enumerate(self._nodes):
            if node.id == node_id:
                return row
        return None

    def activity_text(self, node_id: str) -> str:
        """The recorded summary for ``node_id`` (the placeholder when never run)."""
        return self._activity.get(node_id, NOT_PROBED)

    # -- QAbstractTableModel ------------------------------------------------

    def rowCount(self, parent: QModelIndex | None = None) -> int:
        return 0 if parent is not None and parent.isValid() else len(self._nodes)

    def columnCount(self, parent: QModelIndex | None = None) -> int:
        return 0 if parent is not None and parent.isValid() else len(self.columns)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation is Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.columns[section]
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or role != Qt.ItemDataRole.DisplayRole:
            return None
        if index.row() >= len(self._nodes):
            return None
        node = self._nodes[index.row()]
        return self._cell(node, index.column())

    # -- internals ----------------------------------------------------------

    def _cell(self, node: NodeEntry, column: int) -> str:
        if column == 0:
            if node.name and node.name != node.id:
                return f"{node.name} ({node.id})"
            return node.id
        if column == 1:
            target = f"{node.user}@{node.host}" if node.user else node.host
            return f"{target}:{node.port}" if node.port else target
        if column == 2:
            return _format_roots(node.roots)
        if column == 3:
            return self._activity.get(node.id, NOT_PROBED)
        if column == 4:
            if self._online is None:
                return UNKNOWN
            return ONLINE if node.id in self._online else OFFLINE
        return "-"


def _format_roots(roots: Mapping[str, str]) -> str:
    if not roots:
        return "-"
    return "; ".join(f"{root_id} → {path}" for root_id, path in roots.items())
