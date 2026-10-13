"""Operator stop: a clean local-worker exit renders as stopped, never an error.

Pure Python — runs without the optional PySide6 extra, like the other
supervisor suites. The child here exits on its own (as after a heartbeat stop
or SIGTERM); the supervisor must report the clean exit without restarting it.
"""

from __future__ import annotations

from gui.local_worker_support import (
    FakeProcessTable,
    FakeRegistry,
    FakeServiceRunner,
    FakeSpawner,
    make_config,
)
from videocompressor.gui.local_worker import (
    STATE_ERROR,
    STATE_STOPPED,
    LocalWorkerSupervisor,
)


def _supervisor(tmp_path, *, returncode: int):
    config = make_config(tmp_path)
    spawner = FakeSpawner()
    supervisor = LocalWorkerSupervisor(
        config,
        tmp_path / "config.toml",
        client=FakeRegistry(),
        spawn=spawner,
        run_command=FakeServiceRunner(),
        processes=FakeProcessTable(),
    )
    supervisor.ensure_started()
    assert spawner.calls  # the child started once
    spawner.process.returncode = returncode
    return supervisor, spawner


def test_clean_exit_renders_as_stopped_not_error(tmp_path):
    supervisor, _ = _supervisor(tmp_path, returncode=0)

    outcome = supervisor.status()

    assert outcome.state == STATE_STOPPED
    assert outcome.ok
    assert "stop" in outcome.detail


def test_rendering_a_clean_exit_never_respawns_the_child(tmp_path):
    supervisor, spawner = _supervisor(tmp_path, returncode=0)

    supervisor.status()
    supervisor.status(online=False)

    assert len(spawner.calls) == 1


def test_failed_exit_still_renders_as_error(tmp_path):
    supervisor, _ = _supervisor(tmp_path, returncode=3)

    outcome = supervisor.status()

    assert outcome.state == STATE_ERROR
    assert "code 3" in outcome.detail
