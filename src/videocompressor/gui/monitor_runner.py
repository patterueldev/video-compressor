"""Background monitor polling (issue #9).

A timer triggers one fetch at a time on the thread pool; results come back as
signals, so the GUI thread only ever touches immutable snapshots. Mirrors
:class:`videocompressor.gui.scan_runner.ScanRunner`'s shape (injectable fetch
and pool, one task in flight, results dropped once stopped).
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal

from videocompressor.gui.monitor_client import describe_monitor_error
from videocompressor.gui.monitor_service import MonitorSnapshot

__all__ = ["MonitorRunner"]


class _MonitorTask(QRunnable):
    """One monitor fetch on a pool thread; failures come back as a message."""

    def __init__(self, runner: MonitorRunner) -> None:
        super().__init__()
        self._runner = runner

    def run(self) -> None:
        self._runner.fetch_once()


class MonitorRunner(QObject):
    """Poll the controller on a timer and report snapshots to the GUI thread."""

    updated = Signal(object)  # MonitorSnapshot
    failed = Signal(str)

    def __init__(
        self,
        fetch: Callable[[], MonitorSnapshot],
        *,
        interval_ms: int = 1000,
        parent: QObject | None = None,
        pool: QThreadPool | None = None,
    ) -> None:
        super().__init__(parent)
        self._fetch = fetch
        self._pool = pool if pool is not None else QThreadPool.globalInstance()
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self.refresh_now)
        self._busy = False
        self._active = False

    @property
    def busy(self) -> bool:
        """True while a fetch is in flight."""
        return self._busy

    def start(self) -> None:
        """Start polling: an immediate refresh plus one per interval."""
        self._active = True
        self.refresh_now()
        self._timer.start()

    def stop(self) -> None:
        """Stop polling; an in-flight fetch finishes but its result is dropped."""
        self._active = False
        self._timer.stop()

    def refresh_now(self) -> None:
        """Queue a refresh; no-op while stopped or while one is in flight."""
        if not self._active or self._busy:
            return
        self._busy = True
        self._pool.start(_MonitorTask(self))

    def fetch_once(self) -> None:
        """Run one fetch on the pool thread and emit the outcome."""
        try:
            snapshot = self._fetch()
        except Exception as error:  # surfaced to the user through the runner
            if self._active:
                try:
                    self.failed.emit(describe_monitor_error(error))
                except RuntimeError:  # the window was destroyed mid-fetch
                    pass
        else:
            if self._active:
                try:
                    self.updated.emit(snapshot)
                except RuntimeError:  # the window was destroyed mid-fetch
                    pass
        finally:
            self._busy = False
