"""vc worker CLI wiring: config handling, --once, and signal installation (issue #6)."""

from __future__ import annotations

import os
import signal
import time
from pathlib import Path

import pytest

from fixtures.controller import TOKEN
from fixtures.worker import CLIP
from videocompressor.cli import main
from videocompressor.core.models import Fingerprint, JobState
from videocompressor.worker.pipeline import JobOutcome


def write_worker_config(tmp_path: Path, server, *, pilot: Path | None = None) -> Path:
    archive = tmp_path / "archive"
    archive.mkdir(exist_ok=True)
    scratch = tmp_path / "scratch"
    body = f"""
[worker]
id = "cli-node"
controller_url = "{server.base_url}"
token = "{TOKEN}"
scratch_dir = "{scratch}"
heartbeat_seconds = 0.1

[roots]
archive = "{archive}"
"""
    if pilot is not None:
        body += f'\n[pilot]\nroot = "{pilot}"\n'
    path = tmp_path / "worker.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_worker_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["worker", "--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "--config" in out and "--once" in out


def test_worker_requires_config(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["worker"])
    assert excinfo.value.code == 2
    assert "usage" in capsys.readouterr().err.lower()


def test_worker_missing_config_exits_usage(tmp_path: Path, capsys):
    assert main(["worker", "--config", str(tmp_path / "nope.toml")]) == 2
    assert "does not exist" in capsys.readouterr().err


def test_worker_invalid_config_exits_usage(tmp_path: Path, capsys):
    bad = tmp_path / "bad.toml"
    bad.write_text("[worker\n", encoding="utf-8")
    assert main(["worker", "--config", str(bad)]) == 2
    assert "not valid TOML" in capsys.readouterr().err


class StubPipeline:
    """Pipeline double used to exercise the CLI wiring without ffmpeg."""

    def __init__(self, config, *, on_phase=None, on_progress=None) -> None:
        self.config = config

    def run(self, job, *, stop_event):
        return JobOutcome(
            "complete", output=Fingerprint(size_bytes=1, mtime_ns=1, digest="stub")
        )


def test_worker_once_reports_complete(
    server, seed_clip, manifest, tmp_path: Path, monkeypatch, capsys
):
    seed_clip(CLIP)
    monkeypatch.setattr("videocompressor.worker.agent.JobPipeline", StubPipeline)
    config_path = write_worker_config(tmp_path, server)

    assert main(["worker", "--config", str(config_path), "--once"]) == 0

    assert manifest.get_clip("archive", CLIP).state is JobState.COMPLETE
    assert "cli-node ->" in capsys.readouterr().out


class FakeSignalAgent:
    """Records which stop mode the installed signal handlers requested."""

    def __init__(self) -> None:
        self.stopped = False
        self.drained = False

    def request_stop(self) -> None:
        self.stopped = True

    def request_drain(self) -> None:
        self.drained = True


def _wait_for(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)
    return predicate()


def test_worker_signal_handler_requests_stop():
    from videocompressor.cli.worker import _install_signal_handlers

    agent = FakeSignalAgent()
    restore = _install_signal_handlers(agent)
    try:
        os.kill(os.getpid(), signal.SIGTERM)
        assert _wait_for(lambda: agent.stopped)
        assert not agent.drained
    finally:
        restore()


@pytest.mark.skipif(not hasattr(signal, "SIGUSR1"), reason="platform has no SIGUSR1")
def test_worker_signal_handler_requests_drain():
    from videocompressor.cli.worker import _install_signal_handlers

    agent = FakeSignalAgent()
    restore = _install_signal_handlers(agent)
    try:
        os.kill(os.getpid(), signal.SIGUSR1)
        assert _wait_for(lambda: agent.drained)
        assert not agent.stopped
    finally:
        restore()
