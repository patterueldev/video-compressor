"""Template selector and editor that round-trips the per-user config (issue #8)."""

from __future__ import annotations

from datetime import time
from pathlib import Path

from PySide6.QtCore import QTime, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from videocompressor.core.config import (
    AppConfig,
    add_template,
    duplicate_template,
    rename_template,
    save_config,
    validate_template,
)
from videocompressor.core.models import OriginalHandling, TemplateSettings

#: x265 presets offered in the editor (an editable combo still accepts others).
_PRESETS = (
    "ultrafast",
    "superfast",
    "veryfast",
    "faster",
    "fast",
    "medium",
    "slow",
    "slower",
    "veryslow",
)

#: Human label for the single v1 original-handling mode (read-only in the editor).
_ORIGINAL_LABEL = "Replace source after validated transfer"


class TemplatesPanel(QWidget):
    """The template picker plus the plan.md motorcycle fields.

    Every mutation persists immediately through ``save_config``; validation problems
    leave the file untouched and set :attr:`last_error`.
    """

    template_saved = Signal(str)
    template_selected = Signal(str)

    def __init__(self, config: AppConfig, config_path: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.config = config
        self.config_path = config_path
        self.last_error: str | None = None
        self._original_handling = OriginalHandling.REPLACE_AFTER_VALIDATED_TRANSFER

        self._combo = QComboBox()
        self._height = QSpinBox()
        self._height.setRange(2, 4320)
        self._height.setSingleStep(2)
        self._height.setSuffix(" px")
        self._day = QSpinBox()
        self._day.setRange(1, 100_000)
        self._day.setSuffix(" kbps")
        self._evening = QSpinBox()
        self._evening.setRange(1, 100_000)
        self._evening.setSuffix(" kbps")
        self._evening_start = QTimeEdit()
        self._evening_start.setDisplayFormat("HH:mm")
        self._timezone = QLineEdit()
        self._audio_codec = QLineEdit()
        self._audio_bitrate = QSpinBox()
        self._audio_bitrate.setRange(1, 100_000)
        self._audio_bitrate.setSuffix(" kbps")
        self._preset = QComboBox()
        self._preset.setEditable(True)
        self._preset.addItems(_PRESETS)
        self._original = QLabel(_ORIGINAL_LABEL)
        self._error = QLabel("")
        self._error.setWordWrap(True)

        form = QFormLayout()
        form.addRow("Output height", self._height)
        form.addRow("Daytime bitrate", self._day)
        form.addRow("Evening/night bitrate", self._evening)
        form.addRow("Evening starts", self._evening_start)
        form.addRow("Timezone", self._timezone)
        form.addRow("Audio", self._audio_codec)
        form.addRow("Audio bitrate", self._audio_bitrate)
        form.addRow("FFmpeg preset", self._preset)
        form.addRow("Original handling", self._original)

        buttons = QHBoxLayout()
        for label, handler in (
            ("New…", self._prompt_create),
            ("Duplicate…", self._prompt_duplicate),
            ("Rename…", self._prompt_rename),
            ("Save", self.save_current),
        ):
            button = QPushButton(label)
            button.clicked.connect(handler)
            buttons.addWidget(button)

        outer = QVBoxLayout(self)
        outer.addWidget(QLabel("Processing template"))
        outer.addWidget(self._combo)
        outer.addLayout(form)
        outer.addWidget(self._error)
        outer.addLayout(buttons)
        outer.addStretch(1)
        self._combo.currentTextChanged.connect(self._on_selected)
        self.refresh()

    # -- selection ---------------------------------------------------------

    def names(self) -> list[str]:
        """Template names in config order."""
        return list(self.config.templates)

    def current_name(self) -> str:
        """The selected template name."""
        return self._combo.currentText()

    def current_settings(self) -> TemplateSettings:
        """The selected template's current (saved) settings."""
        return self.config.templates[self.current_name()]

    def select(self, name: str) -> bool:
        """Select ``name`` when it exists; returns whether it was selected."""
        if name not in self.config.templates:
            return False
        self._combo.setCurrentText(name)
        return True

    def refresh(self) -> None:
        """Rebuild the combo from the config and load the current template."""
        current = self.current_name()
        with_signals = self._combo.blockSignals(True)
        try:
            self._combo.clear()
            self._combo.addItems(self.names())
            if current in self.config.templates:
                self._combo.setCurrentText(current)
        finally:
            self._combo.blockSignals(with_signals)
        if self.current_name() in self.config.templates:
            self.load_editor(self.current_settings())

    def load_editor(self, settings: TemplateSettings) -> None:
        """Show ``settings`` in the editor fields."""
        self._height.setValue(settings.output_height)
        self._day.setValue(settings.daytime_video_bitrate_kbps)
        self._evening.setValue(settings.evening_video_bitrate_kbps)
        start = settings.evening_start
        self._evening_start.setTime(QTime(start.hour, start.minute))
        self._timezone.setText(settings.timezone)
        self._audio_codec.setText(settings.audio_codec)
        self._audio_bitrate.setValue(settings.audio_bitrate_kbps)
        self._preset.setCurrentText(settings.ffmpeg_preset)
        self._original_handling = settings.original_handling
        self._original.setText(_ORIGINAL_LABEL)
        self._error.clear()

    # -- editing -----------------------------------------------------------

    def save_current(self) -> bool:
        """Validate the editor and persist the selected template."""
        name = self.current_name()
        if name not in self.config.templates:
            return self._fail(f"unknown template {name!r}")
        settings = self._settings_from_editor()
        problems = validate_template(settings)
        if problems:
            return self._fail("; ".join(problems))
        self.config.templates[name] = settings
        if not self._persist():
            return False
        self.template_saved.emit(name)
        return True

    def create_template(self, name: str) -> bool:
        """Add a template with the motorcycle defaults and select it."""
        try:
            add_template(self.config, name)
        except ValueError as error:
            return self._fail(str(error))
        return self._finish_change(name)

    def duplicate_current(self, new_name: str) -> bool:
        """Copy the selected template under ``new_name`` and select it."""
        try:
            duplicate_template(self.config, self.current_name(), new_name)
        except ValueError as error:
            return self._fail(str(error))
        return self._finish_change(new_name)

    def rename_current(self, new_name: str) -> bool:
        """Rename the selected template and select the new name."""
        try:
            rename_template(self.config, self.current_name(), new_name)
        except ValueError as error:
            return self._fail(str(error))
        return self._finish_change(new_name)

    def _settings_from_editor(self) -> TemplateSettings:
        start = self._evening_start.time()
        return TemplateSettings(
            output_height=self._height.value(),
            daytime_video_bitrate_kbps=self._day.value(),
            evening_video_bitrate_kbps=self._evening.value(),
            evening_start=time(start.hour(), start.minute()),
            timezone=self._timezone.text().strip(),
            audio_codec=self._audio_codec.text().strip(),
            audio_bitrate_kbps=self._audio_bitrate.value(),
            ffmpeg_preset=self._preset.currentText().strip(),
            original_handling=self._original_handling,
        )

    # -- dialogs -----------------------------------------------------------

    def _prompt_create(self) -> None:
        name, accepted = QInputDialog.getText(self, "New template", "Template name")
        if accepted:
            self.create_template(name)

    def _prompt_duplicate(self) -> None:
        current = self.current_name()
        name, accepted = QInputDialog.getText(
            self, "Duplicate template", "New template name", text=f"{current}-copy"
        )
        if accepted:
            self.duplicate_current(name)

    def _prompt_rename(self) -> None:
        current = self.current_name()
        name, accepted = QInputDialog.getText(
            self, "Rename template", "New template name", text=current
        )
        if accepted:
            self.rename_current(name)

    # -- helpers -----------------------------------------------------------

    def _finish_change(self, name: str) -> bool:
        if not self._persist():
            return False
        self.refresh()
        self.select(name)
        self.template_saved.emit(name)
        return True

    def _persist(self) -> bool:
        try:
            save_config(self.config, self.config_path)
        except OSError as error:
            return self._fail(str(error))
        self.last_error = None
        self._error.clear()
        return True

    def _fail(self, message: str) -> bool:
        self.last_error = message
        self._error.setText(message)
        return False

    def _on_selected(self, name: str) -> None:
        if name in self.config.templates:
            self.load_editor(self.config.templates[name])
            self.template_selected.emit(name)
