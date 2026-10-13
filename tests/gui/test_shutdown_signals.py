"""Best-effort SIGTERM/SIGINT quit for the GUI (issue #70).

Pure Python. The tests capture the installed handler by faking ``signal.signal``
and calling it directly; no real signal is ever delivered to the test process.
"""

from __future__ import annotations

import signal

from videocompressor.gui.shutdown_signals import install_shutdown_signals


class SignalRecorder:
    """Captures handlers installed through ``signal.signal``."""

    def __init__(self, previous: dict[int, object] | None = None) -> None:
        self.previous = dict(previous or {})
        self.installed: dict[int, object] = {}

    def getsignal(self, signum: int) -> object:
        return self.previous.get(signum, signal.SIG_DFL)

    def signal(self, signum: int, handler: object) -> None:
        self.installed[signum] = handler


def recorder_patch(monkeypatch, recorder: SignalRecorder) -> None:
    monkeypatch.setattr(signal, "getsignal", recorder.getsignal)
    monkeypatch.setattr(signal, "signal", recorder.signal)


def test_first_signal_stops_then_quits_and_later_signals_are_ignored(monkeypatch):
    calls: list[str] = []
    recorder = SignalRecorder()
    recorder_patch(monkeypatch, recorder)

    install_shutdown_signals(
        lambda: calls.append("stop"), lambda: calls.append("quit")
    )

    assert set(recorder.installed) == {signal.SIGINT, signal.SIGTERM}
    handler = recorder.installed[signal.SIGTERM]
    handler(signal.SIGTERM, None)
    assert calls == ["stop", "quit"]

    handler(signal.SIGTERM, None)  # a re-delivered signal must not re-enter
    assert calls == ["stop", "quit"]


def test_the_sigint_handler_behaves_like_sigterm(monkeypatch):
    calls: list[str] = []
    recorder = SignalRecorder()
    recorder_patch(monkeypatch, recorder)

    install_shutdown_signals(lambda: calls.append("stop"), lambda: calls.append("quit"))
    recorder.installed[signal.SIGINT](signal.SIGINT, None)

    assert calls == ["stop", "quit"]


def test_stop_failure_still_quits(monkeypatch):
    calls: list[str] = []
    recorder = SignalRecorder()
    recorder_patch(monkeypatch, recorder)

    def broken_stop() -> None:
        raise RuntimeError("boom")

    install_shutdown_signals(broken_stop, lambda: calls.append("quit"))
    recorder.installed[signal.SIGTERM](signal.SIGTERM, None)

    assert calls == ["quit"]


def test_restore_reinstalls_the_previous_handlers(monkeypatch):
    previous = {signal.SIGINT: signal.SIG_IGN, signal.SIGTERM: signal.SIG_DFL}
    recorder = SignalRecorder(previous=previous)
    recorder_patch(monkeypatch, recorder)

    restore = install_shutdown_signals(lambda: None, lambda: None)
    restore()

    assert recorder.installed[signal.SIGINT] is signal.SIG_IGN
    assert recorder.installed[signal.SIGTERM] is signal.SIG_DFL


def test_install_is_a_noop_when_signals_are_unavailable(monkeypatch):
    def refuse(signum: int, handler: object) -> None:
        raise ValueError("signal only works in main thread")

    monkeypatch.setattr(signal, "signal", refuse)

    restore = install_shutdown_signals(lambda: None, lambda: None)

    restore()  # nothing installed, nothing to restore, no raise
