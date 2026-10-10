"""Background dry-run execution for the GUI (issue #8).

A scan can take minutes on a real archive, so the window submits it to the thread
pool and keeps the UI responsive. The pure scan work lives in
:mod:`videocompressor.gui.scan_service`; this module only owns the Qt lifecycle.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from videocompressor.gui.scan_service import (
    ScanReport,
    ScanRequest,
    describe_scan_error,
    run_scan,
)


class _ScanTask(QRunnable):
    """One dry run on a pool thread; failures come back as a message."""

    def __init__(self, runner: ScanRunner, request: ScanRequest) -> None:
        super().__init__()
        self._runner = runner
        self._request = request

    def run(self) -> None:
        try:
            report = self._runner.scan(self._request)
        except Exception as error:  # surfaced to the user through the runner
            self._runner.failed.emit(describe_scan_error(error))
        else:
            self._runner.finished.emit(report)


class ScanRunner(QObject):
    """Run one dry run at a time and report the result to the GUI thread."""

    started = Signal()
    finished = Signal(object)  # ScanReport
    failed = Signal(str)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        pool: QThreadPool | None = None,
        scan: Callable[[ScanRequest], ScanReport] = run_scan,
    ) -> None:
        super().__init__(parent)
        self._pool = pool if pool is not None else QThreadPool.globalInstance()
        self.scan = scan
        self._busy = False
        self.finished.connect(self._clear_busy)
        self.failed.connect(self._clear_busy)

    @property
    def busy(self) -> bool:
        """True while a dry run is in flight."""
        return self._busy

    def start(self, request: ScanRequest) -> bool:
        """Submit ``request``; returns ``False`` when a scan is already running."""
        if self._busy:
            return False
        self._busy = True
        self.started.emit()
        self._pool.start(_ScanTask(self, request))
        return True

    def _clear_busy(self, *_result: object) -> None:
        self._busy = False
