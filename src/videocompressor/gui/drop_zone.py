"""The main window's drop target (issue #8)."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QMimeData, Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout, QWidget


def folder_at(mime: QMimeData) -> Path | None:
    """The single local folder carried by ``mime``, or ``None``."""
    if not mime.hasUrls():
        return None
    urls = mime.urls()
    if len(urls) != 1 or not urls[0].isLocalFile():
        return None
    path = Path(urls[0].toLocalFile())
    return path if path.is_dir() else None


class DropZone(QFrame):
    """Accepts exactly one dropped local folder."""

    folder_dropped = Signal(object)  # Path
    folder_rejected = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setMinimumHeight(90)
        label = QLabel("Drag a footage folder here\n(or use Choose folder)")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.addWidget(label)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if folder_at(event.mimeData()) is not None:
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        folder = folder_at(event.mimeData())
        if folder is None:
            self.folder_rejected.emit("drop a single local folder to scan")
            return
        self.folder_dropped.emit(folder)
        event.acceptProposedAction()
