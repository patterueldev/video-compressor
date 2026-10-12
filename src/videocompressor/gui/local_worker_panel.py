"""Local-worker control strip for the Monitor tab (issue #52).

The app runs a worker on this machine by default; this widget is the setting and
the stop/start surface the operator sees, rendered above the node tables. It is
a thin layer over :class:`~videocompressor.gui.local_worker.LocalWorkerSupervisor`:
every action renders the returned outcome, and the online state follows the
monitor's controller snapshots (no extra polling).
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from videocompressor.core.config import (
    AppConfig,
    LocalWorkerSettings,
    save_config,
    validate_local_worker,
)
from videocompressor.gui.local_worker import (
    LocalWorkerOutcome,
    LocalWorkerSupervisor,
    default_scratch_dir,
    local_worker_toml_path,
)
from videocompressor.gui.monitor_service import MonitorSnapshot

__all__ = ["LocalWorkerPanel"]


class LocalWorkerPanel(QWidget):
    """Run/stop the app-managed local worker and show its state."""

    def __init__(
        self,
        supervisor: LocalWorkerSupervisor,
        config: AppConfig,
        config_path: Path,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.supervisor = supervisor
        self.config = config
        self.config_path = config_path
        self.last_error: str | None = None
        self._online = False

        self.enabled_check = QCheckBox("Run a local worker on this machine")
        self.keep_check = QCheckBox("Keep it running when the app closes (service)")
        self.scratch_edit = QLineEdit()
        self.scratch_edit.setToolTip("Local scratch disk for this node; defaults beside the config")
        self.thread_spin = QSpinBox()
        self.thread_spin.setRange(1, 64)
        self.thread_spin.setToolTip("FFmpeg threads for the local worker")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        self.save_button = QPushButton("Save & apply")
        self.start_button = QPushButton("Start")
        self.stop_button = QPushButton("Stop")
        self.status_label = QLabel("")
        self.error_label = QLabel("")
        self.error_label.setWordWrap(True)

        top = QHBoxLayout()
        top.addWidget(self.enabled_check)
        top.addWidget(self.keep_check)
        top.addStretch(1)
        top.addWidget(self.status_label)

        fields = QHBoxLayout()
        fields.addWidget(QLabel("Scratch"))
        fields.addWidget(self.scratch_edit, 1)
        fields.addWidget(browse)
        fields.addSpacing(8)
        fields.addWidget(QLabel("Threads"))
        fields.addWidget(self.thread_spin)
        fields.addSpacing(8)
        fields.addWidget(self.save_button)
        fields.addWidget(self.start_button)
        fields.addWidget(self.stop_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.addLayout(top)
        layout.addLayout(fields)
        layout.addWidget(self.error_label)
        self.setToolTip(f"worker config: {local_worker_toml_path(config_path)}")

        self.save_button.clicked.connect(lambda: self.save())
        self.start_button.clicked.connect(self.start)
        self.stop_button.clicked.connect(self.stop)
        self.refresh()

    # -- public API --------------------------------------------------------

    def refresh(self) -> None:
        """Reload the fields from the config and re-render the status."""
        settings = self.config.local_worker
        self.enabled_check.setChecked(settings.enabled)
        self.keep_check.setChecked(settings.keep_running)
        self.scratch_edit.setText(str(settings.scratch_dir) if settings.scratch_dir else "")
        self.scratch_edit.setPlaceholderText(str(default_scratch_dir(self.config_path)))
        self.thread_spin.setValue(settings.thread_cap)
        self.error_label.clear()
        self._render(self.supervisor.status(online=self._online))

    def save(self) -> bool:
        """Validate the fields, persist the setting, and apply it."""
        settings = self._settings_from_fields()
        problems = validate_local_worker(settings, self.config.roots)
        if problems:
            return self._fail("; ".join(problems))
        self.config.local_worker = settings
        try:
            save_config(self.config, self.config_path)
        except OSError as error:
            return self._fail(str(error))
        self.last_error = None
        self.error_label.clear()
        outcome = self.supervisor.refresh() if settings.enabled else self.supervisor.stop()
        self._render(outcome)
        return True

    def start(self) -> None:
        """Ensure the local worker is running."""
        self._render(self.supervisor.ensure_started())

    def stop(self) -> None:
        """Stop the app-managed local worker."""
        self._render(self.supervisor.stop())

    def on_roots_changed(self) -> None:
        """Apply the new root mapping to the generated worker config."""
        self._render(self.supervisor.refresh())

    def apply_snapshot(self, snapshot: MonitorSnapshot) -> None:
        """Follow the monitor poll: is the configured worker id online?"""
        worker_id = self.config.local_worker.worker_id
        self._online = any(
            worker.worker_id == worker_id and worker.online for worker in snapshot.workers
        )
        self._render(self.supervisor.status(online=self._online))

    # -- internals ---------------------------------------------------------

    def _settings_from_fields(self) -> LocalWorkerSettings:
        text = self.scratch_edit.text().strip()
        return LocalWorkerSettings(
            enabled=self.enabled_check.isChecked(),
            worker_id=self.config.local_worker.worker_id,
            keep_running=self.keep_check.isChecked(),
            scratch_dir=Path(text).expanduser() if text else None,
            thread_cap=self.thread_spin.value(),
        )

    def _render(self, outcome: LocalWorkerOutcome) -> None:
        self.status_label.setText(outcome.detail or outcome.state)

    def _fail(self, message: str) -> bool:
        self.last_error = message
        self.error_label.setText(message)
        return False

    def _browse(self) -> None:
        start = self.scratch_edit.text().strip() or str(default_scratch_dir(self.config_path))
        chosen = QFileDialog.getExistingDirectory(self, "Choose scratch directory", start)
        if chosen:
            self.scratch_edit.setText(chosen)
