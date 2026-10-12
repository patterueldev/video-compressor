"""Background node operations for the Nodes panel (issue #54).

One probe/install/uninstall runs at a time on the thread pool; results come back
as signals, so the GUI thread only ever touches finished values. Mirrors
:class:`videocompressor.gui.scan_runner.ScanRunner`'s shape (injectable pool, one
task in flight, results dropped once stopped).
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

__all__ = ["NodesRunner"]


class _NodesTask(QRunnable):
    """One node operation on a pool thread; the outcome comes back as a signal."""

    def __init__(self, runner: NodesRunner) -> None:
        super().__init__()
        self._runner = runner

    def run(self) -> None:
        self._runner.run_once()


class NodesRunner(QObject):
    """Run one node operation at a time and report the outcome to the GUI thread."""

    started = Signal(str)  # operation label shown in the status line
    finished = Signal(object)  # NodeActivity, or a registry result (set | None)
    failed = Signal(str)

    def __init__(self, parent: QObject | None = None, *, pool: QThreadPool | None = None) -> None:
        super().__init__(parent)
        self._pool = pool if pool is not None else QThreadPool.globalInstance()
        self._call: Callable[[], object] | None = None
        self._busy = False
        self._active = False

    @property
    def busy(self) -> bool:
        """True while an operation is in flight."""
        return self._busy

    def start(self) -> None:
        """Accept operations (idempotent)."""
        self._active = True

    def stop(self) -> None:
        """Stop accepting results; an in-flight operation finishes but is dropped."""
        self._active = False

    def submit(self, label: str, operation: Callable[[], object]) -> bool:
        """Queue one operation; ``False`` while stopped or while one is in flight."""
        if not self._active or self._busy:
            return False
        self._busy = True
        self._call = operation
        self.started.emit(label)
        self._pool.start(_NodesTask(self))
        return True

    def run_once(self) -> None:
        """Run the queued operation on the pool thread and emit the outcome."""
        call = self._call
        if call is None:
            self._busy = False
            return
        try:
            result = call()
        except Exception as error:  # surfaced to the user through the runner
            if self._active:
                try:
                    self.failed.emit(str(error) or type(error).__name__)
                except RuntimeError:  # the window was destroyed mid-operation
                    pass
        else:
            if self._active:
                try:
                    self.finished.emit(result)
                except RuntimeError:  # the window was destroyed mid-operation
                    pass
        finally:
            self._busy = False
            self._call = None
