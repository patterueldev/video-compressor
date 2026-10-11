"""Multi-file/folder drop target for the convert flow (issue #41).

Unlike the Library drop zone (issue #8), which accepts exactly one folder for a
dry run, this one accepts any number of local files and folders; the panel
expands folders and keeps the video files only.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QMimeData, Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout, QWidget

__all__ = ["ConvertDropZone", "local_paths"]


def local_paths(mime: QMimeData) -> list[Path]:
    """Every local file or folder carried by ``mime`` (multiple allowed)."""
    if not mime.hasUrls():
        return []
    return [Path(url.toLocalFile()) for url in mime.urls() if url.isLocalFile()]


class ConvertDropZone(QFrame):
    """Accepts dropped local video files and folders for conversion."""

    paths_dropped = Signal(object)  # list[Path]
    paths_rejected = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setMinimumHeight(80)
        label = QLabel("Drag video files or folders here\n(or use Open files / Open folder)")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.addWidget(label)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if local_paths(event.mimeData()):
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        paths = local_paths(event.mimeData())
        if not paths:
            self.paths_rejected.emit("drop local video files or folders")
            return
        self.paths_dropped.emit(paths)
        event.acceptProposedAction()
