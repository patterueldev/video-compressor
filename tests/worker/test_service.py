"""Service operations with an injected runner and paths (issue #51).

Every test stays offline and platform-injectable: no ``launchctl``/``systemctl``
process is ever spawned.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from videocompressor.worker.config import WorkerConfigError, load_worker_config, read_worker_id
from videocompressor.worker.service import (
    RESULT_ABSENT,
    RESULT_INSTALLED,
    RESULT_REMOVED,
    RESULT_STARTED,
    RESULT_UNCHANGED,
    RESULT_UPDATED,
    STATE_ACTIVE,
    STATE_FAILED,
    STATE_INACTIVE,
    STATE_NOT_INSTALLED,
    CommandResult,
    ServiceError,
    ServicePaths,
    default_worker_command,
    install_service,
    service_status,
    spec_from_config,
    uninstall_service,
)
from videocompressor.worker.units import env_file_path, render_unit, unit_path
from worker.service_support import (
    COMMAND,
    TOKEN,
    MissingBinaryRunner,
    RecordingRunner,
    make_spec,
    probe,
    systemd_show,
    write_worker_config,
)

PLATFORMS = ["macos", "linux"]


def install_calls(platform: str, *, update: bool = False) -> list[tuple[str, ...]]:
    if platform == "macos":
        return [
            ("launchctl", "bootout", "gui/501/com.videocompressor.worker.node-a"),
            ("launchctl", "bootstrap", "gui/501", "UNIT"),
        ]
    unit = "videocompressor-worker-node-a.service"
    if update:
        return [("systemctl", "--user", "daemon-reload"), ("systemctl", "--user", "restart", unit)]
    return [
        ("systemctl", "--user", "daemon-reload"),
        ("systemctl", "--user", "enable", "--now", unit),
    ]


def stop_calls(platform: str) -> list[tuple[str, ...]]:
    if platform == "macos":
        return [("launchctl", "bootout", "gui/501/com.videocompressor.worker.node-a")]
    return [
        ("systemctl", "--user", "disable", "--now", "videocompressor-worker-node-a.service"),
        ("systemctl", "--user", "daemon-reload"),
    ]


def expected_calls(platform: str, spec, paths, *, update: bool = False) -> list[tuple[str, ...]]:
    calls = install_calls(platform, update=update)
    return [
        tuple(str(unit_path(spec, paths)) if part == "UNIT" else part for part in call)
        for call in calls
    ]


def write_unit(spec, paths) -> Path:
    unit = unit_path(spec, paths)
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_bytes(b"unit")
    return unit


@pytest.mark.parametrize("platform", PLATFORMS)
def test_install_fresh_writes_unit_and_starts(tmp_path: Path, paths_for, platform: str) -> None:
    paths = paths_for(platform)
    spec = make_spec(tmp_path / "worker.toml")
    runner = RecordingRunner()

    result = install_service(spec, paths=paths, runner=runner)

    assert result.result == RESULT_INSTALLED
    assert runner.calls == expected_calls(platform, spec, paths)
    assert result.actions == tuple(runner.calls)
    unit = unit_path(spec, paths)
    expected_mode = 0o600 if platform == "macos" else 0o644
    assert unit.stat().st_mode & 0o777 == expected_mode
    payload = result.to_dict()
    assert payload["schema"] == 1
    assert payload["operation"] == "install-service"
    assert payload["platform"] == platform
    assert payload["scope"] == ("launch_agent" if platform == "macos" else "systemd_user")
    assert payload["unit"] == str(unit)


@pytest.mark.parametrize("platform", PLATFORMS)
def test_reinstall_unchanged_when_active(tmp_path: Path, paths_for, platform: str) -> None:
    paths = paths_for(platform)
    spec = make_spec(tmp_path / "worker.toml")
    install_service(spec, paths=paths, runner=RecordingRunner())
    runner = RecordingRunner(by_binary=probe(platform, active=True))

    result = install_service(spec, paths=paths, runner=runner)

    assert result.result == RESULT_UNCHANGED
    assert result.actions == ()
    assert len(runner.calls) == 1  # only the read-only status probe


@pytest.mark.parametrize("platform", PLATFORMS)
def test_reinstall_starts_inactive_service(tmp_path: Path, paths_for, platform: str) -> None:
    paths = paths_for(platform)
    spec = make_spec(tmp_path / "worker.toml")
    install_service(spec, paths=paths, runner=RecordingRunner())
    runner = RecordingRunner(by_binary=probe(platform, active=False))

    result = install_service(spec, paths=paths, runner=runner)

    assert result.result == RESULT_STARTED
    assert list(result.actions) == expected_calls(platform, spec, paths)
    assert runner.calls[-len(result.actions) :] == list(result.actions)


@pytest.mark.parametrize("platform", PLATFORMS)
def test_reinstall_updates_changed_unit(tmp_path: Path, paths_for, platform: str) -> None:
    paths = paths_for(platform)
    spec = make_spec(tmp_path / "worker.toml")
    install_service(spec, paths=paths, runner=RecordingRunner())
    changed = make_spec(tmp_path / "worker.toml", command=("/other/python", *COMMAND[1:]))
    runner = RecordingRunner()

    result = install_service(changed, paths=paths, runner=runner)

    assert result.result == RESULT_UPDATED
    assert runner.calls == expected_calls(platform, spec, paths, update=True)
    document = render_unit(changed, paths)
    assert unit_path(spec, paths).read_bytes() == (
        document if isinstance(document, bytes) else document.encode()
    )


@pytest.mark.parametrize("platform", PLATFORMS)
def test_install_dry_run_writes_nothing(tmp_path: Path, paths_for, platform: str) -> None:
    paths = paths_for(platform)
    spec = make_spec(tmp_path / "worker.toml")
    runner = RecordingRunner()

    result = install_service(spec, paths=paths, runner=runner, dry_run=True)

    assert result.result == RESULT_INSTALLED
    assert result.dry_run is True
    assert list(result.actions) == expected_calls(platform, spec, paths)
    assert runner.calls == []
    assert not unit_path(spec, paths).exists()


@pytest.mark.parametrize("platform", PLATFORMS)
def test_uninstall_removes_then_reports_absent(tmp_path: Path, paths_for, platform: str) -> None:
    paths = paths_for(platform)
    spec = make_spec(tmp_path / "worker.toml")
    install_service(spec, paths=paths, runner=RecordingRunner())
    runner = RecordingRunner()

    result = uninstall_service(spec, paths=paths, runner=runner)

    assert result.result == RESULT_REMOVED
    assert runner.calls == stop_calls(platform)
    assert not unit_path(spec, paths).exists()

    second = RecordingRunner()
    again = uninstall_service(spec, paths=paths, runner=second)
    assert again.result == RESULT_ABSENT
    assert second.calls == []


@pytest.mark.parametrize("platform", PLATFORMS)
def test_status_not_installed_skips_the_probe(tmp_path: Path, paths_for, platform: str) -> None:
    paths = paths_for(platform)
    runner = RecordingRunner()

    status = service_status(make_spec(tmp_path / "worker.toml"), paths=paths, runner=runner)

    assert (status.installed, status.state) == (False, STATE_NOT_INSTALLED)
    assert runner.calls == []


@pytest.mark.parametrize("platform", PLATFORMS)
def test_status_reports_active_and_inactive(tmp_path: Path, paths_for, platform: str) -> None:
    paths = paths_for(platform)
    spec = make_spec(tmp_path / "worker.toml")
    write_unit(spec, paths)

    active = service_status(
        spec, paths=paths, runner=RecordingRunner(by_binary=probe(platform, active=True))
    )
    inactive = service_status(
        spec, paths=paths, runner=RecordingRunner(by_binary=probe(platform, active=False))
    )

    assert (active.installed, active.state) == (True, STATE_ACTIVE)
    assert inactive.state == STATE_INACTIVE


def test_status_failed_on_linux(tmp_path: Path, paths_for) -> None:
    paths = paths_for("linux")
    spec = make_spec(tmp_path / "worker.toml")
    write_unit(spec, paths)
    runner = RecordingRunner(by_binary={"systemctl": systemd_show(active="failed", exec_main="1")})

    status = service_status(spec, paths=paths, runner=runner)

    assert status.state == STATE_FAILED
    assert "ActiveState=failed" in status.detail


def test_status_macos_probe_failure_reports_detail(tmp_path: Path, paths_for) -> None:
    paths = paths_for("macos")
    spec = make_spec(tmp_path / "worker.toml")
    write_unit(spec, paths)
    runner = RecordingRunner(
        by_binary={"launchctl": CommandResult(1, "", "Could not find service")}
    )

    status = service_status(spec, paths=paths, runner=runner)

    assert status.state == STATE_INACTIVE
    assert "Could not find service" in status.detail


def test_linux_token_env_materialized_rotated_and_removed(tmp_path: Path, paths_for) -> None:
    paths = paths_for("linux")
    spec = make_spec(tmp_path / "worker.toml", token_env="VC_WORKER_TOKEN", token_value=TOKEN)
    install_service(spec, paths=paths, runner=RecordingRunner())

    env_file = env_file_path(spec, paths)
    assert env_file is not None
    assert env_file.read_text(encoding="utf-8") == f"VC_WORKER_TOKEN={TOKEN}\n"
    assert env_file.stat().st_mode & 0o777 == 0o600

    rotated = make_spec(
        tmp_path / "worker.toml", token_env="VC_WORKER_TOKEN", token_value="rotated"
    )
    result = install_service(rotated, paths=paths, runner=RecordingRunner())
    assert result.result == RESULT_UPDATED
    assert env_file.read_text(encoding="utf-8") == "VC_WORKER_TOKEN=rotated\n"

    uninstall_service(rotated, paths=paths, runner=RecordingRunner())
    assert not env_file.exists()


def test_install_creates_the_macos_log_dir(tmp_path: Path, paths_for) -> None:
    paths = paths_for("macos")
    assert not paths.log_dir.exists()

    install_service(
        make_spec(tmp_path / "worker.toml"), paths=paths, runner=RecordingRunner()
    )

    assert paths.log_dir.is_dir()


def test_install_dry_run_does_not_create_the_macos_log_dir(tmp_path: Path, paths_for) -> None:
    paths = paths_for("macos")

    install_service(
        make_spec(tmp_path / "worker.toml"), paths=paths, runner=RecordingRunner(), dry_run=True
    )

    assert not paths.log_dir.exists()


def test_uninstall_removes_the_env_file_even_with_an_id_only_spec(
    tmp_path: Path, paths_for
) -> None:
    paths = paths_for("linux")
    spec = make_spec(tmp_path / "worker.toml", token_env="VC_WORKER_TOKEN", token_value=TOKEN)
    install_service(spec, paths=paths, runner=RecordingRunner())
    env_file = env_file_path(spec, paths)
    assert env_file is not None and env_file.is_file()

    # The CLI builds status/uninstall specs from the worker id only.
    light = make_spec(tmp_path / "worker.toml")
    uninstall_service(light, paths=paths, runner=RecordingRunner())

    assert not env_file.exists()


def test_uninstall_cleans_a_leftover_env_file_when_the_unit_is_gone(
    tmp_path: Path, paths_for
) -> None:
    paths = paths_for("linux")
    spec = make_spec(tmp_path / "worker.toml", token_env="VC_WORKER_TOKEN", token_value=TOKEN)
    env_file = env_file_path(spec, paths)
    assert env_file is not None
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text("VC_WORKER_TOKEN=leftover\n", encoding="utf-8")

    result = uninstall_service(spec, paths=paths, runner=RecordingRunner())

    assert result.result == RESULT_ABSENT
    assert not env_file.exists()


def test_status_reports_the_env_file_location(tmp_path: Path, paths_for) -> None:
    paths = paths_for("linux")
    spec = make_spec(tmp_path / "worker.toml", token_env="VC_WORKER_TOKEN", token_value=TOKEN)
    install_service(spec, paths=paths, runner=RecordingRunner())
    light = make_spec(tmp_path / "worker.toml")
    runner = RecordingRunner(by_binary={"systemctl": systemd_show(active="active")})

    status = service_status(light, paths=paths, runner=runner)

    assert status.state == STATE_ACTIVE
    assert status.to_dict()["env_file"] == str(env_file_path(spec, paths))


def test_payloads_never_contain_the_token(tmp_path: Path, paths_for) -> None:
    paths = paths_for("macos")
    spec = make_spec(tmp_path / "worker.toml", token_env="VC_WORKER_TOKEN", token_value=TOKEN)

    result = install_service(spec, paths=paths, runner=RecordingRunner(), dry_run=True)
    status = service_status(spec, paths=paths, runner=RecordingRunner())

    assert TOKEN not in json.dumps(result.to_dict())
    assert TOKEN not in json.dumps(status.to_dict())


def test_missing_manager_binary_raises_service_error(tmp_path: Path, paths_for) -> None:
    with pytest.raises(ServiceError, match="systemctl"):
        install_service(
            make_spec(tmp_path / "worker.toml"),
            paths=paths_for("linux"),
            runner=MissingBinaryRunner(),
        )


def test_failed_command_raises_service_error(tmp_path: Path, paths_for) -> None:
    runner = RecordingRunner(default=CommandResult(1, "", "boom"))
    with pytest.raises(ServiceError, match="boom"):
        install_service(
            make_spec(tmp_path / "worker.toml"), paths=paths_for("linux"), runner=runner
        )


def test_unsupported_platform_raises(tmp_path: Path) -> None:
    paths = ServicePaths(
        platform="windows", home=tmp_path, config_home=tmp_path, log_dir=tmp_path, uid=0
    )
    with pytest.raises(ServiceError, match="unsupported platform"):
        install_service(make_spec(tmp_path / "worker.toml"), paths=paths, runner=RecordingRunner())


def test_default_worker_command_runs_the_cli_module() -> None:
    command = default_worker_command(Path("/abs/worker.toml"))

    assert Path(command[0]).is_absolute()
    assert command[1:5] == ("-m", "videocompressor.cli", "worker", "--config")
    assert command[-1] == "/abs/worker.toml"


def test_spec_from_config_materializes_an_env_token(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("VC_TEST_WORKER_TOKEN", TOKEN)
    config_path = write_worker_config(tmp_path, token_env="VC_TEST_WORKER_TOKEN")

    spec = spec_from_config(load_worker_config(config_path), config_path)

    assert spec.worker_id == "node-a"
    assert spec.token_env == "VC_TEST_WORKER_TOKEN"
    assert spec.token_value == TOKEN


def test_spec_from_config_keeps_an_inline_token_out_of_the_spec(tmp_path: Path) -> None:
    config_path = write_worker_config(tmp_path)

    spec = spec_from_config(load_worker_config(config_path), config_path)

    assert spec.token_env is None
    assert spec.token_value is None


def test_read_worker_id_tolerates_a_broken_config(tmp_path: Path) -> None:
    torn = tmp_path / "torn.toml"
    torn.write_text('[worker]\nid = "node-b"\n', encoding="utf-8")

    assert read_worker_id(torn) == "node-b"
    with pytest.raises(WorkerConfigError):
        load_worker_config(torn)
