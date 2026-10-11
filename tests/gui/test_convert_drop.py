"""Convert drop zone: multiple local files and folders (issue #41)."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6")

import shiboken6
from PySide6.QtCore import QMimeData, QPoint, Qt, QUrl
from PySide6.QtGui import QDropEvent

from videocompressor.gui.convert_drop import ConvertDropZone, local_paths


@pytest.fixture
def zone_factory(qapp):
    """Build drop zones; delete them after the test (see monitor_support_qt.dispose)."""
    created: list[ConvertDropZone] = []

    def build() -> ConvertDropZone:
        zone = ConvertDropZone()
        created.append(zone)
        return zone

    yield build

    for zone in created:
        if shiboken6.isValid(zone):
            shiboken6.delete(zone)


def _mime(*paths: Path) -> QMimeData:
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(path)) for path in paths])
    return mime


def _drop(widget: ConvertDropZone, mime: QMimeData) -> None:
    event = QDropEvent(
        QPoint(5, 5),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    widget.dropEvent(event)


def _sample(tmp_path: Path) -> tuple[Path, Path]:
    folder = tmp_path / "ride"
    folder.mkdir()
    file = tmp_path / "GS010001.MP4"
    file.write_bytes(b"x")
    return file, folder


def test_local_paths_accepts_multiple_files_and_folders(tmp_path, qapp):
    file, folder = _sample(tmp_path)
    assert local_paths(_mime(file, folder)) == [file, folder]


def test_local_paths_rejects_non_local_and_empty(qapp):
    assert local_paths(QMimeData()) == []

    remote = QMimeData()
    remote.setUrls([QUrl("https://example.invalid/clip.mp4")])
    assert local_paths(remote) == []


def test_drop_emits_every_path(tmp_path, zone_factory):
    zone = zone_factory()
    dropped: list[list[Path]] = []
    rejected: list[str] = []
    zone.paths_dropped.connect(dropped.append)
    zone.paths_rejected.connect(rejected.append)

    file, folder = _sample(tmp_path)
    _drop(zone, _mime(file, folder))

    assert dropped == [[file, folder]]
    assert rejected == []


def test_drop_rejects_mime_without_local_files(zone_factory):
    zone = zone_factory()
    rejected: list[str] = []
    zone.paths_rejected.connect(rejected.append)

    _drop(zone, QMimeData())

    assert rejected and "local" in rejected[0]
