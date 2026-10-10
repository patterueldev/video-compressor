"""Qt fakes for the monitor tests (issue #9).

Imported only by Qt test modules after ``pytest.importorskip("PySide6")``, so
CI without the optional ``gui`` extra never loads this module.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, Signal

from videocompressor.core.config import AppConfig
from videocompressor.gui.main_window import MainWindow


class FakeMonitorRunner(QObject):
    """Synchronous stand-in for ``MonitorRunner`` with the same signals and API."""

    updated = Signal(object)
    failed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.started = 0
        self.stopped = 0
        self.refreshes = 0

    def start(self) -> None:
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1

    def refresh_now(self) -> None:
        self.refreshes += 1

    def emit_updated(self, snapshot: object) -> None:
        self.updated.emit(snapshot)

    def emit_failed(self, message: str) -> None:
        self.failed.emit(message)


class FakeScanRunner(QObject):
    """Synchronous stand-in for ``ScanRunner`` with the same signals and API."""

    started = Signal()
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.busy = False
        self.requests: list[object] = []

    def start(self, request: object) -> bool:
        self.busy = True
        self.requests.append(request)
        self.started.emit()
        return True


def make_library(tmp_path: Path, config: AppConfig, runner: FakeScanRunner) -> MainWindow:
    """A ``MainWindow`` wired to the fake scan runner for the window tests."""
    return MainWindow(
        config=config,
        config_path=tmp_path / "config.toml",
        manifest_path=tmp_path / "manifest.sqlite",
        runner=runner,  # type: ignore[arg-type]
    )
