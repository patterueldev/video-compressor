"""Main window: drop, roots, templates, and the dry-run tables (issue #8)."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from videocompressor.core.config import AppConfig, default_manifest_path, root_for_folder
from videocompressor.core.models import ArchiveRoot
from videocompressor.gui.clip_tables import ClipTablesPanel
from videocompressor.gui.drop_zone import DropZone
from videocompressor.gui.roots_panel import RootsPanel
from videocompressor.gui.scan_runner import ScanRunner
from videocompressor.gui.scan_service import ScanRequest
from videocompressor.gui.templates_panel import TemplatesPanel


class MainWindow(QMainWindow):
    """The GUI foundation: pick a folder, scan it read-only, review the rows."""

    def __init__(
        self,
        *,
        config: AppConfig,
        config_path: Path,
        manifest_path: Path | None = None,
        runner: ScanRunner | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Video Compressor")
        self.config = config
        self.config_path = config_path
        self.manifest_path = manifest_path if manifest_path is not None else default_manifest_path()
        self.runner = runner if runner is not None else ScanRunner(self)
        self._selection: tuple[ArchiveRoot, Path] | None = None
        self._last_request: ScanRequest | None = None

        self.drop_zone = DropZone(self)
        self.drop_zone.folder_dropped.connect(self.on_folder_dropped)
        self.drop_zone.folder_rejected.connect(self.statusBar().showMessage)
        choose = QPushButton("Choose folder…")
        choose.clicked.connect(self.choose_folder)
        self.selection_label = QLabel("No footage folder selected")
        self.selection_label.setWordWrap(True)
        side = QVBoxLayout()
        side.addWidget(choose)
        side.addWidget(self.selection_label)
        side.addStretch(1)
        top = QHBoxLayout()
        top.addWidget(self.drop_zone, 1)
        top.addLayout(side)

        self.roots_panel = RootsPanel(
            config, config_path, self, manifest_path=self.manifest_path
        )
        self.templates_panel = TemplatesPanel(config, config_path, self)
        self.templates_panel.template_selected.connect(self._on_template_changed)
        self.templates_panel.template_saved.connect(self._on_template_changed)
        left = QSplitter(Qt.Orientation.Vertical)
        left.addWidget(self.roots_panel)
        left.addWidget(self.templates_panel)

        self.run_button = QPushButton("Run dry run")
        self.run_button.setEnabled(False)
        self.run_button.clicked.connect(self.start_dry_run)
        self.clip_tables = ClipTablesPanel(self)
        right = QVBoxLayout()
        right.addWidget(self.run_button)
        right.addWidget(self.clip_tables, 1)
        right_widget = QWidget()
        right_widget.setLayout(right)

        body = QSplitter(Qt.Orientation.Horizontal)
        body.addWidget(left)
        body.addWidget(right_widget)
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addLayout(top)
        layout.addWidget(body, 1)
        self.setCentralWidget(central)

        self.runner.started.connect(self._on_scan_started)
        self.runner.finished.connect(self._on_scan_finished)
        self.runner.failed.connect(self._on_scan_failed)
        self.statusBar().showMessage("ready")

    # -- folder selection --------------------------------------------------

    def choose_folder(self) -> None:
        """Pick a folder through the dialog and route it like a drop."""
        start = str(self._selection[1]) if self._selection is not None else str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Choose footage folder", start)
        if chosen:
            self.on_folder_dropped(Path(chosen))

    def on_folder_dropped(self, folder: Path) -> None:
        """Resolve ``folder`` against the roots and start its dry run."""
        root = root_for_folder(self.config.roots, folder)
        if root is None:
            self.statusBar().showMessage("folder is not under a registered root; register one")
            registered = self.roots_panel.prompt_register(folder)
            if registered is None:
                self.statusBar().showMessage("no archive root registered; folder ignored")
                return
            root = root_for_folder(self.config.roots, folder)
            if root is None:
                self.statusBar().showMessage(
                    "the dropped folder is not inside the registered root; folder ignored"
                )
                return
        self._selection = (root, folder)
        self.selection_label.setText(f"{root.name} ({root.id}): {folder}")
        self.run_button.setEnabled(True)
        self.start_dry_run()

    # -- dry run -----------------------------------------------------------

    def start_dry_run(self) -> None:
        """Run the read-only scan for the selected folder in the background."""
        if self._selection is None:
            self.statusBar().showMessage("choose a footage folder first")
            return
        root, folder = self._selection
        template_id = self.templates_panel.current_name()
        settings = self.config.templates.get(template_id)
        if settings is None:
            self.show_scan_error(f"unknown template {template_id!r}")
            return
        request = ScanRequest(
            root=root,
            folder=folder,
            template_id=template_id,
            settings=settings,
            capture_mode=self.config.capture_time.mode,
            manifest_path=self.manifest_path,
        )
        if not self.runner.start(request):
            self.statusBar().showMessage("a dry run is already in progress")
            return
        self._last_request = request

    def show_scan_error(self, message: str) -> None:
        """Surface a scan failure without killing the window."""
        self.statusBar().showMessage(message)
        QMessageBox.warning(self, "Video Compressor", message)

    def closeEvent(self, event) -> None:
        """Refuse to close while a scan is still running."""
        if self.runner.busy:
            QMessageBox.information(
                self,
                "Video Compressor",
                "A dry run is still running. Wait for it to finish before closing.",
            )
            event.ignore()
            return
        event.accept()

    # -- runner signals ----------------------------------------------------

    def _on_scan_started(self) -> None:
        self.run_button.setEnabled(False)
        self.statusBar().showMessage("scanning…")

    def _on_scan_finished(self, report) -> None:
        self.run_button.setEnabled(True)
        settings = self._last_request.settings if self._last_request is not None else None
        if settings is not None:
            self.clip_tables.set_report(report, settings)
        self.statusBar().showMessage(f"dry run finished: {len(report.rows)} clips")

    def _on_scan_failed(self, message: str) -> None:
        self.run_button.setEnabled(True)
        self.show_scan_error(message)

    def _on_template_changed(self, name: str) -> None:
        state = self.clip_tables.state()
        settings = self.config.templates.get(name)
        if state is None or settings is None:
            return
        state.set_settings(settings)
        self.clip_tables.refresh()
