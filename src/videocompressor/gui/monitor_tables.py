"""Read-only monitor tables over a snapshot (issue #9).

``NodesModel`` renders every registered node; ``JobsModel`` renders the active
job per node (at most one per worker by design). Both reset wholesale on each
snapshot, in the style of ``clip_tables.ClipTableModel``.
"""

from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, Qt

from videocompressor.gui.monitor_service import (
    MonitorSnapshot,
    WorkerStatus,
    format_eta,
    format_last_seen,
    format_roots,
    format_speed,
)

__all__ = ["JOB_COLUMNS", "NODE_COLUMNS", "JobsModel", "NodesModel"]

NODE_COLUMNS = ("node", "state", "last seen", "current clip", "roots", "stop")
JOB_COLUMNS = ("node", "clip", "phase", "pass", "progress", "speed", "eta")


class _SnapshotModel(QAbstractTableModel):
    """Shared plumbing for the read-only snapshot tables."""

    columns: tuple[str, ...] = ()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._snapshot: MonitorSnapshot | None = None

    def set_snapshot(self, snapshot: MonitorSnapshot | None) -> None:
        """Replace the displayed rows with ``snapshot``."""
        self.beginResetModel()
        self._snapshot = snapshot
        self.endResetModel()

    def worker_at(self, row: int) -> WorkerStatus:
        """The worker backing the displayed row."""
        return self._rows()[row]

    def rowCount(self, parent: QModelIndex | None = None) -> int:
        return 0 if parent is not None and parent.isValid() else len(self._rows())

    def columnCount(self, parent: QModelIndex | None = None) -> int:
        return 0 if parent is not None and parent.isValid() else len(self.columns)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation is Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.columns[section]
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if self._snapshot is None or not index.isValid():
            return None
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        return self.cell(self._snapshot, self._rows()[index.row()], index.column())

    # -- subclass hooks ----------------------------------------------------

    def _rows(self) -> tuple[WorkerStatus, ...]:
        if self._snapshot is None:
            return ()
        return self._snapshot.workers

    def cell(self, snapshot: MonitorSnapshot, worker: WorkerStatus, column: int) -> str:
        raise NotImplementedError


class NodesModel(_SnapshotModel):
    """One row per registered node: state, last seen, clip, mappings, stop."""

    columns = NODE_COLUMNS

    def cell(self, snapshot: MonitorSnapshot, worker: WorkerStatus, column: int) -> str:
        if column == 0:
            if worker.name and worker.name != worker.worker_id:
                return f"{worker.name} ({worker.worker_id})"
            return worker.worker_id
        if column == 1:
            return "online" if worker.online else "offline"
        if column == 2:
            return format_last_seen(worker.last_seen, snapshot.fetched_at)
        if column == 3:
            return worker.active_path or "-"
        if column == 4:
            return format_roots(worker.roots)
        if column == 5:
            return "stopping…" if worker.stop_requested else "-"
        return "-"


class JobsModel(_SnapshotModel):
    """One row per active job: phase, pass, percent, speed, and ETA."""

    columns = JOB_COLUMNS

    def _rows(self) -> tuple[WorkerStatus, ...]:
        return tuple(worker for worker in super()._rows() if worker.active_path is not None)

    def cell(self, snapshot: MonitorSnapshot, worker: WorkerStatus, column: int) -> str:
        progress = worker.progress
        if column == 0:
            return worker.worker_id
        if column == 1:
            return worker.active_path or "-"
        if column == 2:
            return progress.phase if progress is not None and progress.phase else "-"
        if column == 3:
            if progress is not None and progress.pass_number is not None:
                return str(progress.pass_number)
            return "-"
        if column == 4:
            if progress is not None and progress.percent is not None:
                return f"{progress.percent:.1f}%"
            return "-"
        if column == 5:
            return format_speed(progress)
        if column == 6:
            return format_eta(progress.eta_seconds if progress is not None else None)
        return "-"
