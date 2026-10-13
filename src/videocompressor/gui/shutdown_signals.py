"""Best-effort SIGTERM/SIGINT shutdown for the GUI (issue #70).

Pure Python — no PySide6 — so the handler semantics stay testable without the
optional ``gui`` extra. The handler stops the app-managed local worker (a clean
SIGTERM, so the active clip requeues) and asks the event loop to quit; the GUI
entry closes the controller session in its ``finally``. Without it, a signalled
quit (``pkill``, force quit) never runs ``MonitorWindow.closeEvent`` and the
managed child is orphaned.
"""

from __future__ import annotations

import logging
import signal
from collections.abc import Callable

__all__ = ["install_shutdown_signals"]

_log = logging.getLogger("videocompressor.gui")


def install_shutdown_signals(
    stop: Callable[[], None],
    quit_app: Callable[[], None],
    *,
    signals: tuple[int, ...] = (signal.SIGINT, signal.SIGTERM),
) -> Callable[[], None]:
    """Run ``stop`` then ``quit_app`` on the first SIGTERM/SIGINT; return a restore hook.

    Best effort by design: a re-delivered signal is ignored while shutdown runs,
    a failure in ``stop`` is logged but never blocks the quit, and installation
    is skipped without raising when the process is not the main thread or the
    platform refuses the handler.
    """
    fired = False
    previous: dict[int, object] = {}

    def handle(signum: int, frame: object) -> None:
        nonlocal fired
        if fired:
            return
        fired = True
        try:
            stop()
        except Exception:  # a failed stop must not block the quit
            _log.exception("local worker shutdown on signal %s failed", signum)
        try:
            quit_app()
        except Exception:
            _log.exception("could not quit the app after signal %s", signum)

    for signum in signals:
        try:
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, handle)
        except (ValueError, OSError):  # not running in the main thread
            pass

    def restore() -> None:
        for signum, old in previous.items():
            try:
                signal.signal(signum, old)  # type: ignore[arg-type]
            except (ValueError, OSError):
                pass

    return restore
