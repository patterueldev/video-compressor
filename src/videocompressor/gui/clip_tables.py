"""Queue and review tables over :class:`ReportState` (issue #8)."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QAbstractTableModel, QDateTime, QModelIndex, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDateTimeEdit,
    QHeaderView,
    QLabel,
    QSpinBox,
    QSplitter,
    QStyledItemDelegate,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from videocompressor.core.models import TemplateSettings
from videocompressor.core.scan import ScanReport
from videocompressor.gui.state import DisplayRow, ReportState, format_bytes

_COLUMNS = ("clip", "capture time", "bucket", "bitrate", "est. size", "state", "note")
_COL_CLIP, _COL_CAPTURE, _COL_BUCKET, _COL_BITRATE, _COL_EST, _COL_STATE, _COL_NOTE = range(7)


class ClipTableModel(QAbstractTableModel):
    """One table (queue or review) rendering a shared :class:`ReportState`.

    Edits are delegated: the model never mutates rows itself, it asks the state
    and then re-derives the displayed row in place.
    """

    def __init__(self, kind: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.kind = kind
        self._state: ReportState | None = None
        self._rows: tuple[DisplayRow, ...] = ()

    def set_state(self, state: ReportState | None) -> None:
        """Bind (or clear) the report state and rebuild the rows."""
        self.beginResetModel()
        self._state = state
        self.endResetModel()
        self.refresh()

    def refresh(self) -> None:
        """Re-derive the displayed rows from the state after edits or settings changes."""
        self.beginResetModel()
        self._rows = self._rows_for_state()
        self.endResetModel()

    def _rows_for_state(self) -> tuple[DisplayRow, ...]:
        if self._state is None:
            return ()
        return self._state.queue_rows() if self.kind == "queue" else self._state.review_rows()

    def row_at(self, row: int) -> DisplayRow:
        """The displayed row at ``row``."""
        return self._rows[row]

    def rowCount(self, parent: QModelIndex | None = None) -> int:
        return 0 if parent is not None and parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex | None = None) -> int:
        return 0 if parent is not None and parent.isValid() else len(_COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation is Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return _COLUMNS[section]
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            return self._cell(row, index.column(), role)
        return None

    def flags(self, index: QModelIndex):
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        row = self._rows[index.row()]
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if index.column() == _COL_BITRATE and row.editable_bitrate:
            flags |= Qt.ItemFlag.ItemIsEditable
        if index.column() == _COL_CAPTURE and row.editable_capture:
            flags |= Qt.ItemFlag.ItemIsEditable
        return flags

    def setData(self, index: QModelIndex, value, role=Qt.ItemDataRole.EditRole) -> bool:
        if role != Qt.ItemDataRole.EditRole or not index.isValid() or self._state is None:
            return False
        row = self._rows[index.row()]
        if index.column() == _COL_BITRATE and row.editable_bitrate:
            try:
                self._state.set_bitrate(row.relative_path, int(value))
            except (TypeError, ValueError):
                return False
        elif (
            index.column() == _COL_CAPTURE
            and row.editable_capture
            and isinstance(value, datetime)
        ):
            self._state.set_capture_time(row.relative_path, value)
        else:
            return False
        self._rows = self._rows_for_state()
        last = len(_COLUMNS) - 1
        self.dataChanged.emit(self.index(index.row(), 0), self.index(index.row(), last))
        return True

    def _cell(self, row: DisplayRow, column: int, role):
        if column == _COL_CLIP:
            return row.relative_path
        if column == _COL_CAPTURE:
            if row.capture_time is None:
                return "-"
            if role == Qt.ItemDataRole.EditRole:
                return row.capture_time
            text = row.capture_time.isoformat(sep=" ", timespec="minutes")
            return f"{text} (manual)" if row.capture_manual else text
        if column == _COL_BUCKET:
            return row.bucket.value if row.bucket is not None else "-"
        if column == _COL_BITRATE:
            if row.video_bitrate_kbps is None:
                return "-"
            if role == Qt.ItemDataRole.EditRole:
                return int(row.video_bitrate_kbps)
            suffix = " (override)" if row.bitrate_overridden else ""
            return f"{row.video_bitrate_kbps} kbps{suffix}"
        if column == _COL_EST:
            return format_bytes(row.estimated_bytes)
        if column == _COL_STATE:
            return row.classification.value
        return row.note or ""


class BitrateDelegate(QStyledItemDelegate):
    """Spin-box editor for the per-clip bitrate override."""

    def createEditor(self, parent, option, index):
        editor = QSpinBox(parent)
        editor.setRange(100, 100_000)
        editor.setSuffix(" kbps")
        return editor

    def setEditorData(self, editor, index) -> None:
        value = index.data(Qt.ItemDataRole.EditRole)
        if isinstance(value, int):
            editor.setValue(value)

    def setModelData(self, editor, model, index) -> None:
        editor.interpretText()
        model.setData(index, editor.value(), Qt.ItemDataRole.EditRole)


class DateTimeDelegate(QStyledItemDelegate):
    """Date/time editor for a manual capture time on review rows."""

    def createEditor(self, parent, option, index):
        editor = QDateTimeEdit(parent)
        editor.setDisplayFormat("yyyy-MM-dd HH:mm")
        editor.setCalendarPopup(True)
        return editor

    def setEditorData(self, editor, index) -> None:
        value = index.data(Qt.ItemDataRole.EditRole)
        if isinstance(value, datetime):
            editor.setDateTime(
                QDateTime(value.year, value.month, value.day, value.hour, value.minute)
            )
        else:
            editor.setDateTime(QDateTime.currentDateTime())

    def setModelData(self, editor, model, index) -> None:
        value = editor.dateTime()
        when = datetime(
            value.date().year(),
            value.date().month(),
            value.date().day(),
            value.time().hour(),
            value.time().minute(),
        )
        model.setData(index, when, Qt.ItemDataRole.EditRole)


def _table(
    model: ClipTableModel, delegate_column: int, delegate: QStyledItemDelegate
) -> QTableView:
    view = QTableView()
    view.setModel(model)
    view.setItemDelegateForColumn(delegate_column, delegate)
    view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    view.setEditTriggers(
        QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed
    )
    view.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    view.verticalHeader().setVisible(False)
    return view


class ClipTablesPanel(QWidget):
    """The queue and review tables plus the dry-run summary and stale notice."""

    summary_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._state: ReportState | None = None
        self.queue_model = ClipTableModel("queue", self)
        self.review_model = ClipTableModel("review", self)
        self.queue_view = _table(self.queue_model, _COL_BITRATE, BitrateDelegate(self))
        self.review_view = _table(self.review_model, _COL_CAPTURE, DateTimeDelegate(self))
        self.summary = QLabel("No dry run yet")
        self.stale = QLabel("")
        self.stale.setWordWrap(True)

        queue_box = QVBoxLayout()
        queue_box.addWidget(QLabel("Queue"))
        queue_box.addWidget(self.queue_view)
        review_box = QVBoxLayout()
        review_box.addWidget(QLabel("Review"))
        review_box.addWidget(self.review_view)
        queue_widget = QWidget()
        queue_widget.setLayout(queue_box)
        review_widget = QWidget()
        review_widget.setLayout(review_box)

        tables = QSplitter(Qt.Orientation.Vertical)
        tables.addWidget(queue_widget)
        tables.addWidget(review_widget)

        layout = QVBoxLayout(self)
        layout.addWidget(self.summary)
        layout.addWidget(self.stale)
        layout.addWidget(tables, 1)

        for model in (self.queue_model, self.review_model):
            model.dataChanged.connect(self._on_edited)

    def set_report(self, report: ScanReport, settings: TemplateSettings) -> None:
        """Show a finished dry run with the settings it was scanned under."""
        self._state = ReportState(report, settings)
        self.queue_model.set_state(self._state)
        self.review_model.set_state(self._state)
        self._on_edited()

    def state(self) -> ReportState | None:
        """The current report state, or ``None`` before the first dry run."""
        return self._state

    def refresh(self) -> None:
        """Re-derive both tables and the summary (for example after template edits)."""
        self.queue_model.refresh()
        self.review_model.refresh()
        self._on_edited()

    def summary_text(self) -> str:
        """Counts and estimated output for the summary label."""
        if self._state is None:
            return "No dry run yet"
        counts = self._state.counts()
        parts = [f"{count} {kind.value}" for kind, count in counts.items() if count]
        detail = ", ".join(parts) if parts else "no clips"
        total = format_bytes(self._state.estimated_bytes())
        return f"{len(self._state.report.rows)} clips ({detail}); estimated output {total}"

    def stale_notice(self) -> str:
        """A notice when the template changed since the displayed scan."""
        if self._state is not None and self._state.settings_changed:
            return "template changed since this scan — run the dry run again"
        return ""

    def _on_edited(self, *_args: object) -> None:
        self.summary.setText(self.summary_text())
        notice = self.stale_notice()
        self.stale.setText(notice)
        self.stale.setVisible(bool(notice))
        self.summary_changed.emit()
