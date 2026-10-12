"""The application window: convert, library, monitor, and nodes in tabs.

The window embeds the issue #8 :class:`MainWindow` unchanged as the Library tab,
the issue #9 monitor as the Monitor tab, the issue #41 convert flow as the
first tab, and the issue #54 nodes panel as the Nodes tab, so the direct
conversion path is the default view. Closing stops the monitor poller, the nodes
runner, and the hosted controller (an attached controller keeps running); the
issue #8 scan guard is mirrored here because the embedded window no longer
receives its own ``closeEvent``.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QMainWindow, QMessageBox, QTabWidget, QWidget

from videocompressor.core.config import AppConfig
from videocompressor.gui.controller_session import ControllerSession
from videocompressor.gui.convert_panel import ConvertPanel
from videocompressor.gui.main_window import MainWindow
from videocompressor.gui.monitor_panel import MonitorPanel
from videocompressor.gui.monitor_service import MonitorSession
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
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Video Compressor")
        self.session = session
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
        self.monitor = (
            panel
            if panel is not None
            else MonitorPanel(
                MonitorSession(session.client),
                description=session.description,
                parent=self,
            )
        )
        self.convert = (
            convert
            if convert is not None
            else ConvertPanel(session.client, config, parent=self)
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
        self.setCentralWidget(self.tabs)

    # -- convert flow ------------------------------------------------------

    def _on_enqueued(self, _queued: int) -> None:
        """Show the Monitor tab and poll immediately after a successful enqueue."""
        self.tabs.setCurrentWidget(self.monitor)
        self.monitor.refresh_now()

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
        self.nodes.shutdown()
        self.session.close()
        event.accept()
