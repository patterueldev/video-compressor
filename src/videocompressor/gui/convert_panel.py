"""Convert tab: select clips, choose the destination, and enqueue (issue #41).

The panel is a thin rendering layer over :mod:`videocompressor.gui.convert_service`
(pure selection and payload rules) and the controller client (``POST /jobs``,
issue #39). Validating the destination inline and blocking Start keeps the
pipeline-refused combinations out: a same-folder output replaces the source, so
the delete option is forced on and explained. Issue #82 removes the
pre-registration ceremony: dropped or picked folders outside the registered
roots are registered automatically (input at add time, a custom output folder
on Start; see :mod:`videocompressor.gui.root_service`), and the enqueue summary
surfaces configured nodes whose mapping does not cover a root in the batch.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from videocompressor.controller.protocol import ControllerError
from videocompressor.core.config import DEFAULT_TEMPLATE_NAME, AppConfig
from videocompressor.core.models import Destination
from videocompressor.gui.convert_destination import DestinationForm
from videocompressor.gui.convert_drop import ConvertDropZone
from videocompressor.gui.convert_service import (
    MODE_CUSTOM_FOLDER,
    MODE_SAME_FOLDER,
    AddOutcome,
    ConvertSelection,
    build_enqueue_payload,
    effective_delete_original,
)
from videocompressor.gui.date_stamp_runner import BatchSweepDecision, decide_batch_sweep
from videocompressor.gui.monitor_client import MonitorUnavailable, describe_monitor_error
from videocompressor.gui.root_service import (
    format_node_warning,
    missing_node_roots,
    register_roots_for_paths,
    registration_note,
    resolve_destination,
    used_root_ids,
)
from videocompressor.gui.state import format_bytes

__all__ = ["ConvertPanel"]

_DELETE_HINT = (
    "Originals are permanently deleted only after the output exists at the destination"
    " and passes validation."
)
_DELETE_FORCED_HINT = "Replacing in place consumes the original; originals cannot be kept."
_VIDEO_FILTER = "Videos (*.mp4 *.mov)"


class ConvertPanel(QWidget):
    """The convert cockpit: pending list, destination, options, and Start."""

    enqueued = Signal(int)  # newly queued clips; emitted only when > 0
    roots_changed = Signal()  # a folder was auto-registered and persisted

    def __init__(
        self, client, config: AppConfig, config_path: Path, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.client = client
        self.config = config
        self.config_path = config_path
        self.selection = ConvertSelection(lambda: self.config.roots)
        self._delete_choice = False
        self._last_dir = Path.home()
        #: The post-batch sweep decision for the last enqueue (issue #86).
        self.last_batch: BatchSweepDecision | None = None

        # -- selection -----------------------------------------------------
        self.drop_zone = ConvertDropZone(self)
        self.open_files_button = QPushButton("Open files…")
        self.open_folder_button = QPushButton("Open folder…")
        self.clear_button = QPushButton("Clear")
        self.selection_label = QLabel("no videos selected")
        self.pending_table = QTableWidget(0, 3)
        self.pending_table.setHorizontalHeaderLabels(["File", "Size", "Status"])
        self.pending_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.pending_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.pending_table.verticalHeader().setVisible(False)
        self.pending_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.message_label = QLabel("")
        self.message_label.setWordWrap(True)

        # -- destination (own group box; attribute aliases kept for the tests)
        self.destination = DestinationForm(config, self)
        self.mode_combo = self.destination.mode_combo
        self.root_combo = self.destination.root_combo
        self.subfolder_edit = self.destination.subfolder_edit
        self.folder_edit = self.destination.folder_edit
        self.destination_hint = self.destination.hint

        # -- options -------------------------------------------------------
        self.template_combo = QComboBox()
        self.delete_checkbox = QCheckBox("Delete originals after verified conversion")
        self.delete_hint = QLabel(_DELETE_HINT)
        self.delete_hint.setWordWrap(True)
        self.start_button = QPushButton("Start")

        self._build_layout()

        self.drop_zone.paths_dropped.connect(self.add_paths)
        self.drop_zone.paths_rejected.connect(self.message_label.setText)
        self.open_files_button.clicked.connect(self.open_files)
        self.open_folder_button.clicked.connect(self.open_folder)
        self.clear_button.clicked.connect(self.clear_selection)
        self.destination.mode_changed.connect(self._on_mode_changed)
        self.destination.changed.connect(self._revalidate)
        self.template_combo.currentTextChanged.connect(self._update_start_enabled)
        self.delete_checkbox.toggled.connect(self._on_delete_toggled)
        self.start_button.clicked.connect(self.start)

        self.refresh()
        self._on_mode_changed(self.current_mode())

    # -- public API --------------------------------------------------------

    def refresh(self) -> None:
        """Repopulate roots and templates from the live config."""
        self.destination.refresh()
        current_template = self.template_combo.currentText()
        self.template_combo.blockSignals(True)
        try:
            self.template_combo.clear()
            self.template_combo.addItems(list(self.config.templates))
            if current_template in self.config.templates:
                self.template_combo.setCurrentText(current_template)
            elif DEFAULT_TEMPLATE_NAME in self.config.templates:
                self.template_combo.setCurrentText(DEFAULT_TEMPLATE_NAME)
        finally:
            self.template_combo.blockSignals(False)
        self._revalidate()

    def add_paths(self, paths: Iterable[Path]) -> None:
        """Append picked paths to the pending list and refresh the status lines.

        Folders outside the registered roots are registered automatically
        (issue #82) before the selection resolves, so a drop never dead-ends.
        """
        items = list(paths)
        if items:
            first = Path(items[0]).expanduser()
            self._last_dir = first if first.is_dir() else first.parent
        registration = register_roots_for_paths(
            self.config, items, config_path=self.config_path
        )
        if registration.registered:
            self.destination.refresh()
        outcome = self.selection.add_paths(items)
        self._refresh_table()
        self._update_labels()
        parts = [_add_summary(outcome), registration_note(registration)]
        message = ", ".join(part for part in parts if part)
        if not message and items and outcome.added == 0:
            message = "no video files found"
        if message:
            self.message_label.setText(message)
        if registration.registered:
            self.roots_changed.emit()
        self._revalidate()

    def open_files(self) -> None:
        """Pick files through the dialog and add them like a drop."""
        files, _selected = QFileDialog.getOpenFileNames(
            self, "Open videos", str(self._last_dir), _VIDEO_FILTER
        )
        if files:
            self.add_paths(Path(name) for name in files)

    def open_folder(self) -> None:
        """Pick a folder through the dialog and recurse like a dropped folder."""
        folder = QFileDialog.getExistingDirectory(self, "Open video folder", str(self._last_dir))
        if folder:
            self.add_paths([Path(folder)])

    def clear_selection(self) -> None:
        """Empty the pending list."""
        self.selection.clear()
        self._refresh_table()
        self._update_labels()
        self.message_label.clear()
        self._revalidate()

    def start(self) -> None:
        """Validate the form and enqueue the ready entries through the controller API.

        A custom output folder outside the registered roots is registered on
        use (issue #82); the summary then names the new root and any configured
        node whose mapping does not cover a root in the batch.
        """
        mode, root_id, subfolder, folder = self.destination.fields()
        resolution = resolve_destination(
            self.config,
            mode=mode,
            root_id=root_id,
            subfolder=subfolder,
            folder=folder,
            config_path=self.config_path,
        )
        if resolution.problem is not None:
            self._fail(resolution.problem)
            return
        destination = resolution.destination
        assert destination is not None  # guaranteed once ``problem`` is None
        if resolution.registered is not None:
            self.destination.refresh()
            self.roots_changed.emit()
        ready = self.selection.ready_entries()
        if not ready:
            self._fail("select at least one video under a registered root")
            return
        template_id = self.template_combo.currentText()
        if template_id not in self.config.templates:
            self._fail("choose a processing template")
            return
        delete_original = effective_delete_original(mode, self.delete_checkbox.isChecked())
        payload = build_enqueue_payload(
            ready, destination, template_id=template_id, delete_original=delete_original
        )
        enqueued_at = datetime.now().astimezone()
        try:
            body = self.client.enqueue(payload)
        except (ControllerError, MonitorUnavailable, OSError) as error:
            self._fail(describe_monitor_error(error))
            return
        outcome = self.selection.apply_enqueue_result(body)
        self.last_batch = (
            decide_batch_sweep(
                self.config, destination, enqueued_at=enqueued_at, queued=outcome.queued
            )
            if outcome.queued
            else None
        )
        self._refresh_table()
        notes: list[str] = []
        if resolution.registered is not None:
            root = resolution.registered
            notes.append(f"registered {root.name!r} ({root.id}) as an output root")
        note = format_node_warning(
            missing_node_roots(self.config.nodes, used_root_ids(ready, destination))
        )
        if note is not None:
            notes.append(note)
        summary = (
            f"queued {outcome.queued} of {outcome.total} clips"
            f" ({outcome.skipped} skipped, {outcome.rejected} rejected)"
        )
        self.message_label.setText("; ".join([summary, *notes]))
        if outcome.queued > 0:
            self.enqueued.emit(outcome.queued)

    def current_mode(self) -> str:
        """The selected destination mode."""
        return self.destination.mode()

    # -- internals ---------------------------------------------------------

    def _build_layout(self) -> None:
        pick_buttons = QHBoxLayout()
        pick_buttons.addWidget(self.open_files_button)
        pick_buttons.addWidget(self.open_folder_button)
        pick_buttons.addWidget(self.clear_button)
        pick_buttons.addStretch(1)

        options = QVBoxLayout()
        template_row = QHBoxLayout()
        template_row.addWidget(QLabel("Template"))
        template_row.addWidget(self.template_combo, 1)
        options.addLayout(template_row)
        options.addWidget(self.delete_checkbox)
        options.addWidget(self.delete_hint)
        options_box = QGroupBox("Options")
        options_box.setLayout(options)

        layout = QVBoxLayout(self)
        layout.addWidget(self.drop_zone)
        layout.addLayout(pick_buttons)
        layout.addWidget(self.selection_label)
        layout.addWidget(self.pending_table, 1)
        layout.addWidget(self.destination)
        layout.addWidget(options_box)
        layout.addWidget(self.start_button)
        layout.addWidget(self.message_label)

    def _destination(self) -> tuple[Destination | None, str | None]:
        return self.destination.destination()

    def _on_mode_changed(self, mode: str) -> None:
        if mode == MODE_SAME_FOLDER:
            self.delete_checkbox.setEnabled(False)
            self.delete_checkbox.setChecked(True)
            self.delete_hint.setText(_DELETE_FORCED_HINT)
        else:
            self.delete_checkbox.setEnabled(True)
            self.delete_checkbox.setChecked(self._delete_choice)
            self.delete_hint.setText(_DELETE_HINT)
        self._revalidate()

    def _revalidate(self) -> None:
        self._update_start_enabled()

    def _update_start_enabled(self) -> None:
        destination, problem = self._destination()
        resolvable = destination is not None and problem is None
        if not resolvable:
            resolvable = self._custom_folder_registrable()
        self.start_button.setEnabled(
            resolvable
            and bool(self.selection.ready_entries())
            and self.template_combo.currentText() in self.config.templates
        )

    def _custom_folder_registrable(self) -> bool:
        """Whether Start may register the picked custom folder on use (#82)."""
        if self.current_mode() != MODE_CUSTOM_FOLDER:
            return False
        text = self.folder_edit.text().strip()
        return bool(text) and Path(text).expanduser().is_dir()

    def _on_delete_toggled(self, checked: bool) -> None:
        if self.delete_checkbox.isEnabled():
            self._delete_choice = checked

    def _refresh_table(self) -> None:
        entries = self.selection.entries()
        self.pending_table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            name = entry.relative_path or str(entry.path)
            status = entry.status or entry.problem or ("ready" if entry.ready else "")
            for column, text in enumerate((name, format_bytes(entry.size_bytes), status)):
                self.pending_table.setItem(row, column, QTableWidgetItem(text))

    def _update_labels(self) -> None:
        entries = self.selection.entries()
        if not entries:
            self.selection_label.setText("no videos selected")
            return
        attention = sum(1 for entry in entries if not entry.ready)
        text = f"{len(entries)} files · {format_bytes(self.selection.total_bytes())}"
        if attention:
            text += f" · {attention} need attention"
        self.selection_label.setText(text)

    def _fail(self, message: str) -> None:
        self.message_label.setText(message)
        QMessageBox.warning(self, "Video Compressor", message)


def _add_summary(outcome: AddOutcome) -> str | None:
    """One status line for a selection addition; ``None`` when there is nothing to say."""
    parts: list[str] = []
    if outcome.added:
        parts.append(f"added {_plural(outcome.added, 'video')}")
    if outcome.ignored_non_video:
        parts.append(f"ignored {_plural(outcome.ignored_non_video, 'non-video file')}")
    if outcome.duplicates:
        parts.append(f"skipped {_plural(outcome.duplicates, 'already-selected file')}")
    if not parts:
        return None
    if outcome.added == 0:
        return "no new videos: " + ", ".join(parts)
    return ", ".join(parts)


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" + ("" if count == 1 else "s")
