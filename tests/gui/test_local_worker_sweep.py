"""Startup stale-child sweep for the app-managed local worker (issue #70).

Pure Python — runs without the optional PySide6 extra. No test spawns, signals,
or enumerates a real process: every supervisor gets a fake process table, and
the timeout paths run on a fake clock.
"""

from __future__ import annotations

import signal
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from gui.local_worker_support import (
    FakeClock,
    FakeProcessTable,
    FakeRegistry,
    FakeServiceRunner,
    FakeSpawner,
    make_config,
    worker_entry,
)
from videocompressor.core.config import AppConfig
from videocompressor.gui import local_worker as local_worker_module
from videocompressor.gui import local_worker_processes as processes_module
from videocompressor.gui.local_worker import (
    CHILD_WAIT_SECONDS,
    KILL_WAIT_SECONDS,
    STATE_ALREADY_RUNNING,
    STATE_STARTED_CHILD,
    STATE_STARTED_SERVICE,
    LocalWorkerSupervisor,
    local_worker_toml_path,
    worker_command,
)
from videocompressor.gui.local_worker_processes import (
    PS_ARGV,
    ProcessInfo,
    SystemProcessTable,
    parse_ps,
    stale_worker_pids,
    terminate_stale_workers,
)


def stale_command(config_path: Path) -> str:
    """The argv line a managed child shows in ``ps`` (same argv as the real spawn)."""
    return " ".join(worker_command(local_worker_toml_path(config_path)))


def supervisor_for(
    tmp_path: Path,
    config: AppConfig,
    *,
    table: FakeProcessTable | None = None,
    registry: FakeRegistry | None = None,
    runner: FakeServiceRunner | None = None,
    spawner: FakeSpawner | None = None,
) -> tuple[LocalWorkerSupervisor, FakeSpawner, FakeServiceRunner, FakeRegistry, FakeProcessTable]:
    """A supervisor with recording fakes; returns (supervisor, spawner, runner, registry, table)."""
    table = table if table is not None else FakeProcessTable()
    spawner = spawner if spawner is not None else FakeSpawner()
    runner = runner if runner is not None else FakeServiceRunner()
    registry = registry if registry is not None else FakeRegistry()
    supervisor = LocalWorkerSupervisor(
        config,
        tmp_path / "config.toml",
        client=registry,
        spawn=spawner,
        run_command=runner,
        processes=table,
    )
    return supervisor, spawner, runner, registry, table


# -- detection -----------------------------------------------------------------


def test_detection_matches_only_the_generated_config(tmp_path):
    generated = local_worker_toml_path(tmp_path / "config.toml")
    rows = [
        ProcessInfo(11, stale_command(tmp_path / "config.toml")),
        ProcessInfo(12, stale_command(tmp_path / "config.toml").replace("--config ", "--config=")),
        ProcessInfo(13, " ".join(worker_command(tmp_path / "standalone-worker.toml"))),
        ProcessInfo(14, f"cat {generated.resolve()}"),
        ProcessInfo(15, "python3 -m http.server --config unrelated.toml"),
        ProcessInfo(16, stale_command(tmp_path / "config.toml")),
    ]

    pids = stale_worker_pids(rows, generated, exclude_pids={16})

    assert pids == [11, 12]


def test_standalone_workers_and_other_commands_are_never_signalled(tmp_path):
    config = make_config(tmp_path)
    generated = local_worker_toml_path(tmp_path / "config.toml")
    table = FakeProcessTable(
        [
            (4242, stale_command(tmp_path / "config.toml")),
            (4343, " ".join(worker_command(tmp_path / "standalone-worker.toml"))),
            (4444, f"cat {generated.resolve()}"),
        ]
    )
    supervisor, spawner, *_ = supervisor_for(tmp_path, config, table=table)

    outcome = supervisor.ensure_started()

    assert outcome.state == STATE_STARTED_CHILD
    assert table.signals == [(4242, signal.SIGTERM)]
    assert [row.pid for row in table.list_processes()] == [4343, 4444]
    assert len(spawner.calls) == 1


# -- supervisor wiring ---------------------------------------------------------


def test_stale_child_is_terminated_before_the_new_child_starts(tmp_path):
    config = make_config(tmp_path)
    events: list[str] = []
    table = FakeProcessTable(
        [(4242, stale_command(tmp_path / "config.toml"))], events=events
    )
    supervisor, spawner, *_ = supervisor_for(
        tmp_path, config, table=table, spawner=FakeSpawner(events=events)
    )

    outcome = supervisor.ensure_started()

    assert outcome.state == STATE_STARTED_CHILD
    assert table.signals == [(4242, signal.SIGTERM)]
    assert len(spawner.calls) == 1
    assert events.index(f"signal:4242:{signal.SIGTERM}") < events.index("spawn")
    assert table.list_processes() == ()


def test_killed_child_registration_does_not_block_the_restart(tmp_path):
    config = make_config(tmp_path)
    table = FakeProcessTable([(4242, stale_command(tmp_path / "config.toml"))])
    registry = FakeRegistry(
        [worker_entry("local", last_seen=datetime.now(UTC) - timedelta(seconds=5))]
    )
    supervisor, spawner, *_ = supervisor_for(
        tmp_path, config, table=table, registry=registry
    )

    outcome = supervisor.ensure_started()

    assert outcome.state == STATE_STARTED_CHILD
    assert len(spawner.calls) == 1
    assert (tmp_path / "local-worker.state.json").is_file()


def test_no_stale_process_starts_cleanly_without_a_marker(tmp_path):
    config = make_config(tmp_path)
    supervisor, spawner, _runner, _registry, table = supervisor_for(tmp_path, config)

    outcome = supervisor.ensure_started()

    assert outcome.state == STATE_STARTED_CHILD
    assert len(spawner.calls) == 1
    assert table.signals == []
    assert not (tmp_path / "local-worker.state.json").exists()


def test_keep_running_never_sweeps_processes(tmp_path):
    config = make_config(tmp_path, keep_running=True)
    table = FakeProcessTable([(4242, stale_command(tmp_path / "config.toml"))])
    supervisor, spawner, *_ = supervisor_for(tmp_path, config, table=table)

    outcome = supervisor.ensure_started()

    assert outcome.state == STATE_STARTED_SERVICE
    assert table.signals == []
    assert spawner.calls == []


def test_unconfirmed_sweep_leaves_the_registry_row_authoritative(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    registry = FakeRegistry([worker_entry("local")])
    table = FakeProcessTable([(4242, stale_command(tmp_path / "config.toml"))])
    monkeypatch.setattr(local_worker_module, "terminate_stale_workers", lambda *args, **kw: [])
    supervisor, spawner, *_ = supervisor_for(
        tmp_path, config, table=table, registry=registry
    )

    outcome = supervisor.ensure_started()

    assert outcome.state == STATE_ALREADY_RUNNING
    assert spawner.calls == []
    assert not (tmp_path / "local-worker.state.json").exists()


# -- termination decision --------------------------------------------------------


def test_stubborn_worker_is_killed_after_the_grace(tmp_path):
    generated = local_worker_toml_path(tmp_path / "config.toml")
    table = FakeProcessTable(
        [(4242, stale_command(tmp_path / "config.toml"))], ignore_term={4242}
    )
    clock = FakeClock()

    terminated = terminate_stale_workers(
        generated,
        table=table,
        terminate_timeout=CHILD_WAIT_SECONDS,
        kill_timeout=KILL_WAIT_SECONDS,
        sleep=clock.sleep,
        clock=clock,
    )

    assert terminated == [4242]
    assert table.signals == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]
    assert clock.now >= CHILD_WAIT_SECONDS  # waited out the SIGTERM grace


def test_surviving_worker_is_not_reported_as_terminated(tmp_path):
    generated = local_worker_toml_path(tmp_path / "config.toml")
    table = FakeProcessTable(
        [(4242, stale_command(tmp_path / "config.toml"))], ignore_all={4242}
    )
    clock = FakeClock()

    terminated = terminate_stale_workers(
        generated,
        table=table,
        terminate_timeout=1.0,
        kill_timeout=1.0,
        sleep=clock.sleep,
        clock=clock,
    )

    assert terminated == []
    assert table.signals == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]
    assert clock.now >= 2.0


def test_a_vanished_process_counts_as_gone(tmp_path):
    generated = local_worker_toml_path(tmp_path / "config.toml")

    class VanishingTable(FakeProcessTable):
        def signal(self, pid: int, signum: int) -> None:
            self.signals.append((pid, signum))
            self._rows.pop(pid, None)
            raise ProcessLookupError(pid, "No such process")

    table = VanishingTable([(4242, stale_command(tmp_path / "config.toml"))])

    terminated = terminate_stale_workers(
        generated,
        table=table,
        terminate_timeout=1.0,
        kill_timeout=1.0,
        sleep=lambda _seconds: None,
        clock=lambda: 0.0,
    )

    assert terminated == [4242]
    assert table.signals == [(4242, signal.SIGTERM)]


def test_permission_errors_are_tolerated_and_not_confirmed(tmp_path):
    generated = local_worker_toml_path(tmp_path / "config.toml")

    class UnkillableTable(FakeProcessTable):
        def signal(self, pid: int, signum: int) -> None:
            self.signals.append((pid, signum))
            raise PermissionError(f"cannot signal {pid}")

    table = UnkillableTable([(4242, stale_command(tmp_path / "config.toml"))])
    clock = FakeClock()

    terminated = terminate_stale_workers(
        generated,
        table=table,
        terminate_timeout=1.0,
        kill_timeout=1.0,
        sleep=clock.sleep,
        clock=clock,
    )

    assert terminated == []
    assert table.signals == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]


# -- the default table -----------------------------------------------------------


def test_parse_ps_reads_pid_and_command():
    output = "\n   1 /sbin/launchd\n  544 /usr/libexec/logd (x)\nnot a row\n"

    assert parse_ps(output) == (
        ProcessInfo(1, "/sbin/launchd"),
        ProcessInfo(544, "/usr/libexec/logd (x)"),
    )


def test_system_process_table_runs_ps_and_parses(monkeypatch):
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def fake_run(argv, **kwargs):
        calls.append((tuple(argv), kwargs))
        return subprocess.CompletedProcess(argv, 0, "  7 worker --config /x\n", "")

    monkeypatch.setattr(processes_module.subprocess, "run", fake_run)

    assert SystemProcessTable().list_processes() == (ProcessInfo(7, "worker --config /x"),)
    assert calls[0][0] == PS_ARGV
    assert calls[0][1] == {"capture_output": True, "text": True, "check": False}


def test_system_process_table_degrades_to_an_empty_table(monkeypatch):
    def failing_run(argv, **kwargs):
        raise OSError("no ps")

    monkeypatch.setattr(processes_module.subprocess, "run", failing_run)
    assert SystemProcessTable().list_processes() == ()

    monkeypatch.setattr(
        processes_module.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1, "", "ps: bad"),
    )
    assert SystemProcessTable().list_processes() == ()
