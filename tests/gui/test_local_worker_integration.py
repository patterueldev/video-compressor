"""Integration: the supervisor starts a real worker against the controller (issue #52).

No ffmpeg and no media: the worker registers with an in-process controller,
polls for jobs, and is stopped again — the app-managed default path end to end.
"""

from __future__ import annotations

import time
from pathlib import Path

from fixtures.controller import TOKEN
from gui.local_worker_support import FakeProcessTable, make_config
from videocompressor.gui.local_worker import (
    STATE_STARTED_CHILD,
    STATE_STOPPED,
    LocalWorkerSupervisor,
)
from videocompressor.gui.local_worker_service import spawn_child
from videocompressor.gui.monitor_client import HttpControllerClient


def wait_until(predicate, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_supervisor_starts_a_real_worker_and_stops_it(server, tmp_path: Path):
    config = make_config(tmp_path)
    config.controller.host = "127.0.0.1"
    config.controller.port = server.port
    config.controller.token = TOKEN

    client = HttpControllerClient(f"http://127.0.0.1:{server.port}", TOKEN)
    spawned = []

    def recording_spawn(argv, env, log_dir):
        process = spawn_child(argv, env, log_dir)
        spawned.append(process)
        return process

    supervisor = LocalWorkerSupervisor(
        config,
        tmp_path / "config.toml",
        client=client,
        spawn=recording_spawn,
        processes=FakeProcessTable(),  # hermetic: never enumerate real processes
    )
    try:
        outcome = supervisor.ensure_started()
        assert outcome.state == STATE_STARTED_CHILD

        def worker_entry():
            entries = client.workers().get("workers", [])
            return next(
                (entry for entry in entries if entry.get("worker_id") == "local"), None
            )

        assert wait_until(lambda: (entry := worker_entry()) and entry.get("online")), (
            "the local worker never registered with the controller"
        )
        assert set(worker_entry()["roots"]) == {"archive"}

        # a second ensure adopts the running child instead of spawning a duplicate
        assert supervisor.ensure_started().state == STATE_STARTED_CHILD
        assert len(spawned) == 1

        assert supervisor.stop().state == STATE_STOPPED
        assert wait_until(lambda: spawned[0].poll() is not None)
        assert spawned[0].returncode == 0

        # restarting right after a confirmed stop must start exactly one again,
        # even though the registry row is still fresh for the lease window
        assert supervisor.ensure_started().state == STATE_STARTED_CHILD
        assert len(spawned) == 2
    finally:
        supervisor.stop()
