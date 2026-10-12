"""Local-worker supervisor (issue #52).

Pure Python — runs without the optional PySide6 extra.
"""

from __future__ import annotations

import json
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path

from gui.local_worker_support import (
    FakeRegistry,
    FakeServiceRunner,
    FakeSpawner,
    StubbornProcess,
    make_config,
    roots_payload,
    worker_entry,
)
from videocompressor.core.config import AppConfig, register_root
from videocompressor.gui.local_worker import (
    STATE_ALREADY_RUNNING,
    STATE_DISABLED,
    STATE_ERROR,
    STATE_NO_ROOTS,
    STATE_STARTED_CHILD,
    STATE_STARTED_SERVICE,
    STATE_STOPPED,
    LocalWorkerSupervisor,
    local_worker_toml_path,
    worker_command,
)
from videocompressor.gui.local_worker_config import ERR_LOG_FILENAME, TOKEN_ENV_VAR
from videocompressor.gui.monitor_client import MonitorUnavailable


def supervisor_for(
    tmp_path: Path,
    config: AppConfig,
    *,
    registry: FakeRegistry | None = None,
    spawner: FakeSpawner | None = None,
    runner: FakeServiceRunner | None = None,
    config_path: Path | None = None,
) -> tuple[LocalWorkerSupervisor, FakeSpawner, FakeServiceRunner, FakeRegistry]:
    """A supervisor with recording fakes; returns (supervisor, spawner, runner, registry)."""
    spawner = spawner if spawner is not None else FakeSpawner()
    runner = runner if runner is not None else FakeServiceRunner()
    registry = registry if registry is not None else FakeRegistry()
    supervisor = LocalWorkerSupervisor(
        config,
        config_path if config_path is not None else tmp_path / "config.toml",
        client=registry,
        spawn=spawner,
        run_command=runner,
    )
    return supervisor, spawner, runner, registry


# -- starting -----------------------------------------------------------------


def test_disabled_setting_does_nothing(tmp_path):
    config = make_config(tmp_path, enabled=False)
    supervisor, spawner, runner, registry = supervisor_for(tmp_path, config)

    outcome = supervisor.ensure_started()

    assert outcome.state == STATE_DISABLED
    assert spawner.calls == []
    assert runner.calls == []
    assert registry.calls == 0
    assert not local_worker_toml_path(tmp_path / "config.toml").exists()


def test_missing_roots_reports_the_setting(tmp_path):
    config = make_config(tmp_path, roots=False)
    supervisor, spawner, *_ = supervisor_for(tmp_path, config)

    outcome = supervisor.ensure_started()

    assert outcome.state == STATE_NO_ROOTS
    assert spawner.calls == []


def test_missing_token_is_an_error(tmp_path):
    config = make_config(tmp_path, token=None)
    supervisor, spawner, *_ = supervisor_for(tmp_path, config)

    outcome = supervisor.ensure_started()

    assert outcome.state == STATE_ERROR
    assert spawner.calls == []


def test_starts_a_child_with_the_generated_config(tmp_path):
    config = make_config(tmp_path, thread_cap=3)
    supervisor, spawner, runner, _ = supervisor_for(tmp_path, config)

    outcome = supervisor.ensure_started()

    assert outcome.state == STATE_STARTED_CHILD
    assert len(spawner.calls) == 1
    argv, env, log_dir = spawner.calls[0]
    assert argv == worker_command(supervisor.worker_config_path())
    assert argv[:3] == (argv[0], "-m", "videocompressor.cli")
    assert env[TOKEN_ENV_VAR] == "test-token"
    assert log_dir == tmp_path
    assert runner.operations == ["service-status"]  # the leftover check only
    config_file = supervisor.worker_config_path()
    assert config_file.is_file()
    assert config_file.stat().st_mode & 0o777 == 0o600
    assert "test-token" not in config_file.read_text(encoding="utf-8")


def test_ensure_started_is_idempotent_for_a_live_child(tmp_path):
    config = make_config(tmp_path)
    supervisor, spawner, *_ = supervisor_for(tmp_path, config)

    assert supervisor.ensure_started().state == STATE_STARTED_CHILD
    assert supervisor.ensure_started().state == STATE_STARTED_CHILD

    assert len(spawner.calls) == 1
    assert spawner.process.terminate_calls == 0


def test_unchanged_config_is_not_rewritten(tmp_path):
    config = make_config(tmp_path)
    supervisor, *_ = supervisor_for(tmp_path, config)
    supervisor.ensure_started()
    path = supervisor.worker_config_path()
    first_bytes = path.read_bytes()
    path.chmod(0o644)

    supervisor.refresh()

    assert path.read_bytes() == first_bytes
    assert path.stat().st_mode & 0o777 == 0o600  # private again, bytes untouched


# -- idempotency ---------------------------------------------------------------


def test_same_id_online_is_adopted(tmp_path):
    config = make_config(tmp_path)
    registry = FakeRegistry([worker_entry("local")])
    supervisor, spawner, *_ = supervisor_for(tmp_path, config, registry=registry)

    outcome = supervisor.ensure_started()

    assert outcome.state == STATE_ALREADY_RUNNING
    assert spawner.calls == []


def test_same_roots_under_another_id_is_adopted(tmp_path):
    config = make_config(tmp_path)
    registry = FakeRegistry([worker_entry("macbook", roots=roots_payload(config))])
    supervisor, spawner, *_ = supervisor_for(tmp_path, config, registry=registry)

    outcome = supervisor.ensure_started()

    assert outcome.state == STATE_ALREADY_RUNNING
    assert "macbook" in outcome.detail
    assert spawner.calls == []


def test_offline_duplicate_does_not_block(tmp_path):
    config = make_config(tmp_path)
    registry = FakeRegistry([worker_entry("local", online=False)])
    supervisor, spawner, *_ = supervisor_for(tmp_path, config, registry=registry)

    assert supervisor.ensure_started().state == STATE_STARTED_CHILD
    assert len(spawner.calls) == 1


def test_a_row_older_than_a_confirmed_stop_is_started_again(tmp_path):
    config = make_config(tmp_path)
    stopped_at = datetime.now(UTC)
    (tmp_path / "local-worker.state.json").write_text(
        json.dumps({"stopped_at": stopped_at.isoformat()}), encoding="utf-8"
    )
    registry = FakeRegistry([worker_entry("local", last_seen=stopped_at - timedelta(seconds=30))])
    supervisor, spawner, *_ = supervisor_for(tmp_path, config, registry=registry)

    assert supervisor.ensure_started().state == STATE_STARTED_CHILD
    assert len(spawner.calls) == 1


def test_a_row_newer_than_the_marker_is_still_live(tmp_path):
    config = make_config(tmp_path)
    stopped_at = datetime.now(UTC) - timedelta(minutes=5)
    (tmp_path / "local-worker.state.json").write_text(
        json.dumps({"stopped_at": stopped_at.isoformat()}), encoding="utf-8"
    )
    registry = FakeRegistry([worker_entry("local", last_seen=datetime.now(UTC))])
    supervisor, spawner, *_ = supervisor_for(tmp_path, config, registry=registry)

    assert supervisor.ensure_started().state == STATE_ALREADY_RUNNING
    assert spawner.calls == []


def test_registry_unreachable_fails_closed(tmp_path):
    config = make_config(tmp_path)
    registry = FakeRegistry(error=MonitorUnavailable("down"))
    supervisor, spawner, *_ = supervisor_for(tmp_path, config, registry=registry)

    outcome = supervisor.ensure_started()

    assert outcome.state == STATE_ERROR
    assert "registry" in outcome.detail
    assert spawner.calls == []


# -- service mode --------------------------------------------------------------


def test_keep_running_installs_the_service_without_a_child(tmp_path):
    config = make_config(tmp_path, keep_running=True)
    runner = FakeServiceRunner(installed=False)
    supervisor, spawner, runner, registry = supervisor_for(tmp_path, config, runner=runner)

    outcome = supervisor.ensure_started()

    assert outcome.state == STATE_STARTED_SERVICE
    assert outcome.detail == "service started"
    assert spawner.calls == []
    assert runner.operations == ["service-status", "install-service"]
    assert runner.calls[1][1][TOKEN_ENV_VAR] == "test-token"
    assert registry.calls == 1


def test_keep_running_refreshes_an_installed_service(tmp_path):
    config = make_config(tmp_path, keep_running=True)
    runner = FakeServiceRunner(installed=True)
    supervisor, spawner, runner, registry = supervisor_for(tmp_path, config, runner=runner)

    outcome = supervisor.ensure_started()

    assert outcome.state == STATE_STARTED_SERVICE
    assert runner.operations == ["service-status", "install-service"]
    assert spawner.calls == []
    assert registry.calls == 0  # an installed service is authoritative


def test_keep_running_skips_when_an_external_child_is_online(tmp_path):
    config = make_config(tmp_path, keep_running=True)
    registry = FakeRegistry([worker_entry("local")])
    supervisor, spawner, runner, _ = supervisor_for(tmp_path, config, registry=registry)

    outcome = supervisor.ensure_started()

    assert outcome.state == STATE_ALREADY_RUNNING
    assert runner.operations == ["service-status"]
    assert spawner.calls == []


def test_child_mode_uninstalls_a_leftover_service_first(tmp_path):
    config = make_config(tmp_path)
    registry = FakeRegistry([worker_entry("local", last_seen=datetime.now(UTC))])
    runner = FakeServiceRunner(installed=True)
    supervisor, spawner, runner, _ = supervisor_for(
        tmp_path, config, registry=registry, runner=runner
    )

    outcome = supervisor.ensure_started()

    assert outcome.state == STATE_STARTED_CHILD
    assert runner.operations == ["service-status", "uninstall-service"]
    assert len(spawner.calls) == 1
    assert (tmp_path / "local-worker.state.json").is_file()  # the stop is on record


def test_service_install_failure_is_reported_without_the_token(tmp_path):
    config = make_config(tmp_path, keep_running=True)
    runner = FakeServiceRunner(failing={"install-service"})
    supervisor, *_ = supervisor_for(tmp_path, config, runner=runner)

    outcome = supervisor.ensure_started()

    assert outcome.state == STATE_ERROR
    assert "install-service failed" in outcome.detail
    assert "test-token" not in outcome.detail


# -- stopping ------------------------------------------------------------------


def test_stop_terminates_the_child_and_removes_the_service(tmp_path):
    config = make_config(tmp_path)
    supervisor, spawner, runner, _ = supervisor_for(tmp_path, config)
    supervisor.ensure_started()

    outcome = supervisor.stop()

    assert outcome.state == STATE_STOPPED
    assert spawner.process.terminate_calls == 1
    assert runner.operations == ["service-status", "uninstall-service"]  # start, then stop
    assert (tmp_path / "local-worker.state.json").is_file()
    assert not supervisor.managed_child_alive()


def test_stop_kills_a_stubborn_child(tmp_path):
    config = make_config(tmp_path)
    spawner = FakeSpawner(StubbornProcess())
    supervisor, spawner, runner, _ = supervisor_for(tmp_path, config, spawner=spawner)
    supervisor.ensure_started()

    outcome = supervisor.stop()

    assert outcome.state == STATE_STOPPED
    assert spawner.process.kill_calls == 1


def test_stop_without_anything_running_is_idempotent(tmp_path):
    config = make_config(tmp_path)
    supervisor, spawner, runner, _ = supervisor_for(tmp_path, config)

    assert supervisor.stop().state == STATE_STOPPED
    assert supervisor.stop().state == STATE_STOPPED
    assert runner.calls == []  # no generated config, nothing to uninstall
    assert spawner.calls == []


def test_stop_reports_a_failed_uninstall(tmp_path):
    config = make_config(tmp_path)
    runner = FakeServiceRunner(failing={"uninstall-service"})
    supervisor, *_ = supervisor_for(tmp_path, config, runner=runner)
    supervisor.ensure_started()

    outcome = supervisor.stop()

    assert outcome.state == STATE_ERROR
    assert "uninstall-service failed" in outcome.detail


def test_shutdown_stops_the_child_but_leaves_the_service(tmp_path):
    config = make_config(tmp_path)
    supervisor, spawner, runner, _ = supervisor_for(tmp_path, config)
    supervisor.ensure_started()

    supervisor.shutdown()

    assert spawner.process.terminate_calls == 1
    assert "uninstall-service" not in runner.operations

    service_config = make_config(tmp_path / "svc", keep_running=True)
    service_supervisor, service_spawner, service_runner, _ = supervisor_for(
        tmp_path / "svc", service_config, runner=FakeServiceRunner(installed=True)
    )
    service_supervisor.ensure_started()
    service_supervisor.shutdown()
    assert service_spawner.calls == []
    assert "uninstall-service" not in service_runner.operations


# -- refresh and status --------------------------------------------------------


def test_refresh_restarts_the_child_after_a_root_change(tmp_path):
    config = make_config(tmp_path)
    supervisor, spawner, *_ = supervisor_for(tmp_path, config)
    supervisor.ensure_started()
    extra = tmp_path / "output"
    extra.mkdir()
    register_root(config, root_id="output", name="Output", path=extra)

    outcome = supervisor.refresh()

    assert outcome.state == STATE_STARTED_CHILD
    assert len(spawner.calls) == 2
    assert spawner.process.terminate_calls == 1
    generated = tomllib.loads(supervisor.worker_config_path().read_text(encoding="utf-8"))
    assert set(generated["roots"]) == {"archive", "output"}


def test_refresh_reinstalls_the_service_after_a_root_change(tmp_path):
    config = make_config(tmp_path, keep_running=True)
    runner = FakeServiceRunner(installed=True)
    supervisor, *_ = supervisor_for(tmp_path, config, runner=runner)
    supervisor.ensure_started()
    extra = tmp_path / "output"
    extra.mkdir()
    register_root(config, root_id="output", name="Output", path=extra)

    outcome = supervisor.refresh()

    assert outcome.state == STATE_STARTED_SERVICE
    assert runner.operations == [
        "service-status",
        "install-service",
        "service-status",
        "install-service",
    ]


def test_refresh_with_the_setting_off_stops_the_worker(tmp_path):
    config = make_config(tmp_path)
    supervisor, spawner, *_ = supervisor_for(tmp_path, config)
    supervisor.ensure_started()
    config.local_worker.enabled = False

    outcome = supervisor.refresh()

    assert outcome.state == STATE_STOPPED
    assert spawner.process.terminate_calls == 1


def test_status_reports_a_dead_child_with_its_log(tmp_path):
    config = make_config(tmp_path)
    supervisor, spawner, *_ = supervisor_for(tmp_path, config)
    supervisor.ensure_started()
    spawner.process.returncode = 3

    outcome = supervisor.status()

    assert outcome.state == STATE_ERROR
    assert "code 3" in outcome.detail
    assert ERR_LOG_FILENAME in outcome.detail


def test_status_marks_external_and_service_online_states(tmp_path):
    config = make_config(tmp_path)
    supervisor, *_ = supervisor_for(tmp_path, config)
    assert supervisor.status(online=False).state == STATE_STOPPED
    assert supervisor.status(online=True).state == STATE_ALREADY_RUNNING

    service_config = make_config(tmp_path / "svc", keep_running=True)
    service_supervisor, *_ = supervisor_for(tmp_path / "svc", service_config)
    assert service_supervisor.status(online=True).state == STATE_STARTED_SERVICE
    assert service_supervisor.status(online=True).detail == "online (service)"
