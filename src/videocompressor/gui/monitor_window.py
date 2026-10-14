"""The application window: convert, library, monitor, and nodes in tabs.

The window embeds the issue #8 :class:`MainWindow` unchanged as the Library tab,
the issue #9 monitor as the Monitor tab, the issue #41 convert flow as the
first tab, and the issue #54 nodes panel as the Nodes tab, so the direct
conversion path is the default view. Closing stops the monitor poller, the nodes
runner, the local worker child (unless it keeps running), and the hosted
controller (an attached controller keeps running); the issue #8 scan guard is
mirrored here because the embedded window no longer receives its own
``closeEvent``.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QMainWindow, QMessageBox, QTabWidget, QWidget

from videocompressor.core.config import AppConfig
from videocompressor.core.date_stamp import summarize
from videocompressor.gui.controller_session import ControllerSession
from videocompressor.gui.convert_panel import ConvertPanel
from videocompressor.gui.date_stamp_runner import (
    BatchSweepWatcher,
    DateStampRunner,
    settled_total,
    sweep_request_for,
)
from videocompressor.gui.local_worker import LocalWorkerSupervisor
from videocompressor.gui.local_worker_panel import LocalWorkerPanel
from videocompressor.gui.main_window import MainWindow
from videocompressor.gui.monitor_panel import MonitorPanel
from videocompressor.gui.monitor_service import MonitorSession, MonitorSnapshot
from videocompressor.gui.nodes_panel import NodesPanel

__all__ = ["MonitorWindow"]

_SCAN_RUNNING = "A dry run is still running. Wait for it to finish before closing."
_HOSTED_CLOSE = (
    "A job is running on the hosted controller. Closing stops the controller;"
    " the active job becomes recoverable. Close anyway?"
)


class MonitorWindow(QMainWindow):
    """The GUI shell: Convert, Library, and Monitor tabs."""

    def __init__(
        self,
        *,
        config: AppConfig,
        config_path: Path,
        manifest_path: Path | None,
        session: ControllerSession,
        library: MainWindow | None = None,
        panel: MonitorPanel | None = None,
        convert: ConvertPanel | None = None,
        nodes: NodesPanel | None = None,
        date_runner: DateStampRunner | None = None,
        supervisor: LocalWorkerSupervisor | None = None,
        local_worker: QWidget | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Video Compressor")
        self.session = session
        self.supervisor = supervisor
        self.library = (
            library
            if library is not None
            else MainWindow(
                config=config,
                config_path=config_path,
                manifest_path=manifest_path,
                parent=self,
            )
        )
        self.local_worker = (
            local_worker
            if local_worker is not None
            else (
                LocalWorkerPanel(supervisor, config, config_path, parent=self)
                if supervisor is not None
                else None
            )
        )
        self.monitor = (
            panel
            if panel is not None
            else MonitorPanel(
                MonitorSession(session.client),
                description=session.description,
                header=self.local_worker,
                parent=self,
            )
        )
        self.convert = (
            convert
            if convert is not None
            else ConvertPanel(session.client, config, config_path, parent=self)
        )
        self.nodes = (
            nodes
            if nodes is not None
            else NodesPanel(
                config=config,
                config_path=config_path,
                client=session.client,
                parent=self,
            )
        )
        self.tabs = QTabWidget(self)
        self.tabs.addTab(self.convert, "Convert")
        self.tabs.addTab(self.library, "Library")
        self.tabs.addTab(self.monitor, "Monitor")
        self.tabs.addTab(self.nodes, "Nodes")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.convert.enqueued.connect(self._on_enqueued)
        self.convert.roots_changed.connect(self._on_convert_roots_changed)
        self.setCentralWidget(self.tabs)
        self._batch_sweep: BatchSweepWatcher | None = None
        self.date_runner = date_runner if date_runner is not None else DateStampRunner(self)
        self.date_runner.finished.connect(self._on_dates_stamped)
        self.date_runner.failed.connect(self._on_dates_failed)
        monitor_runner = getattr(self.monitor, "runner", None)
        if monitor_runner is not None:
            monitor_runner.updated.connect(self._on_batch_snapshot)
        if self.local_worker is not None:
            self.monitor.runner.updated.connect(self.local_worker.apply_snapshot)
            roots_panel = getattr(self.library, "roots_panel", None)
            if roots_panel is not None:
                roots_panel.roots_changed.connect(self.local_worker.on_roots_changed)

    # -- convert flow ------------------------------------------------------

    def _on_enqueued(self, _queued: int) -> None:
        """Show the Monitor tab, poll immediately, and arm the sweep (issue #86)."""
        self.tabs.setCurrentWidget(self.monitor)
        self.monitor.refresh_now()
        self._arm_batch_sweep()

    def _arm_batch_sweep(self) -> None:
        """Arm the post-batch creation-date sweep for the batch just enqueued."""
        self._batch_sweep = None
        decision = getattr(self.convert, "last_batch", None)
        if decision is None:
            return
        if decision.note is not None:
            self.statusBar().showMessage(decision.note)
            return
        plan = decision.plan
        if plan is None:
            return
        snapshot_getter = getattr(self.monitor, "snapshot", None)
        snapshot = snapshot_getter() if callable(snapshot_getter) else None
        baseline = settled_total(snapshot) if snapshot is not None else 0
        self._batch_sweep = BatchSweepWatcher(plan=plan, baseline_settled=baseline)

    def _on_batch_snapshot(self, snapshot: MonitorSnapshot) -> None:
        """Start the armed sweep once the monitor shows the queue drained."""
        watcher = self._batch_sweep
        if watcher is None:
            return
        plan = watcher.observe(snapshot)
        if plan is None:
            return
        self._batch_sweep = None
        self.statusBar().showMessage(f"creation dates: checking {plan.folder}…")
        self.date_runner.start(sweep_request_for(plan))

    def _on_dates_stamped(self, report) -> None:
        """Report a finished sweep in the status area; never a dialog."""
        self.statusBar().showMessage(f"creation dates: {summarize(report)}")

    def _on_dates_failed(self, message: str) -> None:
        """Report a failure without blocking the UI."""
        self.statusBar().showMessage(f"creation dates: {message}")

    def _on_convert_roots_changed(self) -> None:
        """Follow a convert-flow auto-registration in the Library and local worker."""
        roots_panel = getattr(self.library, "roots_panel", None)
        if roots_panel is not None:
            roots_panel.refresh()
        if self.local_worker is not None:
            self.local_worker.on_roots_changed()

    def _on_tab_changed(self, _index: int) -> None:
        """Refresh the convert form when its tab becomes current."""
        if self.tabs.currentWidget() is self.convert:
            self.convert.refresh()
        elif self.tabs.currentWidget() is self.nodes:
            self.nodes.refresh_list()
            self.nodes.refresh_registry()

    def closeEvent(self, event) -> None:
        """Guard an active scan, confirm a hosted-controller close, then settle."""
        if self.library.runner.busy:
            QMessageBox.information(self, "Video Compressor", _SCAN_RUNNING)
            event.ignore()
            return
        if self.session.hosted and self.monitor.has_active_jobs():
            answer = QMessageBox.question(self, "Video Compressor", _HOSTED_CLOSE)
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        self.monitor.shutdown()
        if self.supervisor is not None:
            self.supervisor.shutdown()
        self.nodes.shutdown()
        self.session.close()
        event.accept()
