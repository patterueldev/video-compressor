"""Drop zone: a single local folder, nothing else (issue #8)."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QMimeData, QPoint, Qt, QUrl
from PySide6.QtGui import QDropEvent

from videocompressor.gui.drop_zone import DropZone, folder_at


def _mime(*paths: Path) -> QMimeData:
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(path)) for path in paths])
    return mime


def _drop(widget: DropZone, mime: QMimeData) -> None:
    event = QDropEvent(
        QPoint(5, 5),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    widget.dropEvent(event)


def test_folder_at_accepts_one_directory(tmp_path, qapp):
    folder = tmp_path / "ride"
    folder.mkdir()
    assert folder_at(_mime(folder)) == folder


def test_folder_at_rejects_files_multiple_and_non_local(tmp_path, qapp):
    file = tmp_path / "clip.mp4"
    file.write_bytes(b"x")
    folder = tmp_path / "ride"
    folder.mkdir()

    assert folder_at(_mime(file)) is None
    assert folder_at(_mime(folder, file)) is None
    assert folder_at(QMimeData()) is None

    remote = QMimeData()
    remote.setUrls([QUrl("https://example.invalid/clip.mp4")])
    assert folder_at(remote) is None


def test_drop_emits_the_folder(tmp_path, qapp):
    zone = DropZone()
    dropped: list[Path] = []
    rejected: list[str] = []
    zone.folder_dropped.connect(dropped.append)
    zone.folder_rejected.connect(rejected.append)

    folder = tmp_path / "ride"
    folder.mkdir()
    _drop(zone, _mime(folder))

    assert dropped == [folder]
    assert rejected == []


def test_drop_rejects_a_file(tmp_path, qapp):
    zone = DropZone()
    rejected: list[str] = []
    zone.folder_rejected.connect(rejected.append)

    file = tmp_path / "clip.mp4"
    file.write_bytes(b"x")
    _drop(zone, _mime(file))

    assert rejected and "folder" in rejected[0]
