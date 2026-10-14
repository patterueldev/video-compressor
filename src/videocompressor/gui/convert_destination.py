"""Destination controls for the convert panel (issue #41).

The group box owns the three destination modes, the root/subfolder/custom-folder
inputs, the inline validation hint, and the selected template's output-name
preview (issue #87). It emits :attr:`changed` on every edit and
:attr:`mode_changed` when the mode itself moves, so the panel can force the
delete option for same-folder and re-evaluate Start without reaching into the
widgets.
"""

from __future__ import annotations

from datetime import datetime
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
from videocompressor.core.models import Destination, TemplateSettings
from videocompressor.core.naming import (
    NamePatternError,
    needs_capture_time,
    render_output_name,
)
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
    "Outputs go inside the root subfolder, which is created when needed; an empty"
    " subfolder mirrors each clip's source folders under the root."
)
HINT_CUSTOM_FOLDER = (
    "Outputs are placed under this folder; a folder outside the registered roots"
    " is registered as an output root when you press Start."
)
HINT_SAME_FOLDER = (
    "The output replaces the source at its own path (verified swap); the original"
    " cannot be kept."
)

#: Sample clip and capture instant for the output-name preview.
_SAMPLE_NAME = "GS010001"
_SAMPLE_WHEN = datetime(2025, 4, 19, 21, 17, 5)


class DestinationForm(QGroupBox):
    """The destination group box with inline validation."""

    changed = Signal()
    mode_changed = Signal(str)

    def __init__(self, config: AppConfig, parent: QWidget | None = None) -> None:
        super().__init__("Destination", parent)
        self.config = config
        self._template_settings: TemplateSettings | None = None
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
        self.name_preview = QLabel("")
        self.name_preview.setWordWrap(True)
        self.root_row = _row("Output root", self.root_combo)
        self.subfolder_row = _row("Subfolder", self.subfolder_edit)
        self.custom_row = _row("Output folder", self.folder_edit, self.folder_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self.mode_combo)
        layout.addWidget(self.root_row)
        layout.addWidget(self.subfolder_row)
        layout.addWidget(self.custom_row)
        layout.addWidget(self.hint)
        layout.addWidget(self.name_preview)

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

    def fields(self) -> tuple[str, str | None, str, Path | None]:
        """The raw destination inputs: mode, root id, subfolder, folder."""
        root_id = self.root_combo.currentData()
        folder_text = self.folder_edit.text().strip()
        return (
            self.mode(),
            root_id if isinstance(root_id, str) else None,
            self.subfolder_edit.text(),
            Path(folder_text).expanduser() if folder_text else None,
        )

    def destination(self) -> tuple[Destination | None, str | None]:
        """The current destination, or ``(None, problem)`` when invalid."""
        mode, root_id, subfolder, folder = self.fields()
        return destination_from_fields(
            mode=mode,
            root_id=root_id,
            subfolder=subfolder,
            folder=folder,
            roots=self.config.roots,
        )

    def show_template(self, settings: TemplateSettings | None) -> None:
        """Show the selected template's pattern and a sample output name (#87)."""
        self._template_settings = settings
        self._refresh_name_preview()

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
        self._refresh_name_preview()
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

    def _refresh_name_preview(self) -> None:
        visible = self.mode() != MODE_SAME_FOLDER
        self.name_preview.setVisible(visible)
        self.name_preview.setText(_name_preview(self._template_settings) if visible else "")

    def _browse_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose output folder", "")
        if folder:
            self.folder_edit.setText(folder)


def _name_preview(settings: TemplateSettings | None) -> str:
    """One line naming the effective pattern and a sample output file name."""
    if settings is None:
        return ""
    pattern = settings.name_pattern
    when = _SAMPLE_WHEN if needs_capture_time(pattern) else None
    try:
        rendered = render_output_name(pattern, name=_SAMPLE_NAME, when=when)
    except NamePatternError:
        return f"Output names: pattern {pattern!r} is invalid; clips keep their names."
    return f"Output names: {pattern} → {rendered}.MP4"


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
