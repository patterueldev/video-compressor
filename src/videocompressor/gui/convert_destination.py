"""Destination controls for the convert panel (issue #41).

The group box owns the three destination modes, the root/subfolder/custom-folder
inputs, and the inline validation hint. It emits :attr:`changed` on every edit
and :attr:`mode_changed` when the mode itself moves, so the panel can force the
delete option for same-folder and re-evaluate Start without reaching into the
widgets.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from videocompressor.core.config import AppConfig
from videocompressor.core.models import Destination
from videocompressor.gui.convert_service import (
    MODE_CUSTOM_FOLDER,
    MODE_OUTPUT_ROOT,
    MODE_SAME_FOLDER,
    destination_from_fields,
)

__all__ = ["DestinationForm"]

_MODE_LABELS = (
    ("Registered output root", MODE_OUTPUT_ROOT),
    ("Custom folder", MODE_CUSTOM_FOLDER),
    ("Same folder as source", MODE_SAME_FOLDER),
)

HINT_OUTPUT_ROOT = (
    "Outputs go under the root plus the subfolder; an empty subfolder mirrors each"
    " clip's source folders."
)
HINT_CUSTOM_FOLDER = "Outputs are placed in this folder; it must be inside a registered root."
HINT_SAME_FOLDER = (
    "The output replaces the source at its own path (verified swap); the original"
    " cannot be kept."
)


class DestinationForm(QGroupBox):
    """The destination group box with inline validation."""

    changed = Signal()
    mode_changed = Signal(str)

    def __init__(self, config: AppConfig, parent: QWidget | None = None) -> None:
        super().__init__("Destination", parent)
        self.config = config
        self.mode_combo = QComboBox()
        for label, mode in _MODE_LABELS:
            self.mode_combo.addItem(label, mode)
        self.root_combo = QComboBox()
        self.subfolder_edit = QLineEdit()
        self.subfolder_edit.setPlaceholderText("optional; empty mirrors the source folders")
        self.folder_edit = QLineEdit()
        self.folder_button = QPushButton("Browse…")
        self.hint = QLabel("")
        self.hint.setWordWrap(True)
        self.root_row = _row("Output root", self.root_combo)
        self.subfolder_row = _row("Subfolder", self.subfolder_edit)
        self.custom_row = _row("Output folder", self.folder_edit, self.folder_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self.mode_combo)
        layout.addWidget(self.root_row)
        layout.addWidget(self.subfolder_row)
        layout.addWidget(self.custom_row)
        layout.addWidget(self.hint)

        self.mode_combo.currentIndexChanged.connect(self._on_mode)
        self.root_combo.currentIndexChanged.connect(self._on_edit)
        self.subfolder_edit.textChanged.connect(self._on_edit)
        self.folder_edit.textChanged.connect(self._on_edit)
        self.folder_button.clicked.connect(self._browse_folder)

        self._apply_visibility()
        self.refresh()

    # -- public API --------------------------------------------------------

    def mode(self) -> str:
        """The selected destination mode."""
        return str(self.mode_combo.currentData())

    def destination(self) -> tuple[Destination | None, str | None]:
        """The current destination, or ``(None, problem)`` when invalid."""
        root_id = self.root_combo.currentData()
        folder_text = self.folder_edit.text().strip()
        return destination_from_fields(
            mode=self.mode(),
            root_id=root_id if isinstance(root_id, str) else None,
            subfolder=self.subfolder_edit.text(),
            folder=Path(folder_text).expanduser() if folder_text else None,
            roots=self.config.roots,
        )

    def refresh(self) -> None:
        """Repopulate the root combo from the live config."""
        current = self.root_combo.currentData()
        self.root_combo.blockSignals(True)
        try:
            self.root_combo.clear()
            for root in self.config.roots:
                self.root_combo.addItem(f"{root.name} ({root.id})", root.id)
            index = self.root_combo.findData(current)
            if index >= 0:
                self.root_combo.setCurrentIndex(index)
        finally:
            self.root_combo.blockSignals(False)
        self._refresh_hint()

    # -- internals ---------------------------------------------------------

    def _on_mode(self) -> None:
        self._apply_visibility()
        self._refresh_hint()
        self.mode_changed.emit(self.mode())

    def _on_edit(self) -> None:
        self._refresh_hint()
        self.changed.emit()

    def _apply_visibility(self) -> None:
        mode = self.mode()
        self.root_row.setVisible(mode == MODE_OUTPUT_ROOT)
        self.subfolder_row.setVisible(mode == MODE_OUTPUT_ROOT)
        self.custom_row.setVisible(mode == MODE_CUSTOM_FOLDER)

    def _refresh_hint(self) -> None:
        _destination, problem = self.destination()
        self.hint.setText(problem if problem is not None else _hint_for_mode(self.mode()))

    def _browse_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose output folder", "")
        if folder:
            self.folder_edit.setText(folder)


def _hint_for_mode(mode: str) -> str:
    if mode == MODE_CUSTOM_FOLDER:
        return HINT_CUSTOM_FOLDER
    if mode == MODE_SAME_FOLDER:
        return HINT_SAME_FOLDER
    return HINT_OUTPUT_ROOT


def _row(label: str, *widgets: QWidget) -> QWidget:
    """One labelled destination row (hidden as a unit per mode)."""
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(QLabel(label))
    for widget in widgets:
        layout.addWidget(widget, 1)
    return row
