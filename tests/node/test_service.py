"""The frozen #51 service surface (issue #53)."""

from __future__ import annotations

import shlex

import pytest

from fixtures.node import CONFIG_PATH, VENV_DIR, FakeSshRunner, service_status_json
from videocompressor.node.models import CommandResult, ServiceError
from videocompressor.node.service import (
    STATUS_SETTLE_SECONDS,
    build_service_command,
    build_status_command,
    confirmed_service_status,
    install_service,
    parse_service_status,
    service_status,
    uninstall_service,
)


def test_build_service_command_quotes_paths() -> None:
    command = build_service_command(VENV_DIR, "install-service", CONFIG_PATH)
    assert shlex.split(command) == [
        f"{VENV_DIR}/bin/vc",
        "worker",
        "install-service",
        "--config",
        CONFIG_PATH,
    ]


def test_build_service_command_quotes_paths_with_spaces() -> None:
    command = build_service_command("/opt/vc dir/venv", "service-status", "/opt/vc dir/w.toml")
    assert shlex.split(command) == [
        "/opt/vc dir/venv/bin/vc",
        "worker",
        "service-status",
        "--config",
        "/opt/vc dir/w.toml",
    ]


def test_install_service_failure_raises() -> None:
    runner = FakeSshRunner().on("install-service", 1)
    with pytest.raises(ServiceError, match="install-service failed"):
        install_service(runner, venv_dir=VENV_DIR, config_path=CONFIG_PATH)


def test_install_service_success_returns_the_result() -> None:
    runner = FakeSshRunner()
    result = install_service(runner, venv_dir=VENV_DIR, config_path=CONFIG_PATH)
    assert result.returncode == 0
    assert "worker install-service --config" in runner.commands[-1]


def test_service_status_returns_the_raw_result() -> None:
    runner = FakeSshRunner(service_rc=3, service_out="not running")
    result = service_status(runner, venv_dir=VENV_DIR, config_path=CONFIG_PATH)
    assert result.returncode == 3
    assert result.stdout == "not running"
    assert "worker service-status --config" in runner.commands[-1]
    assert runner.commands[-1].endswith("--json")


def test_build_status_command_requests_json() -> None:
    command = build_status_command(VENV_DIR, CONFIG_PATH)
    assert shlex.split(command) == [
        f"{VENV_DIR}/bin/vc",
        "worker",
        "service-status",
        "--config",
        CONFIG_PATH,
        "--json",
    ]


def test_parse_service_status_active() -> None:
    state = parse_service_status(CommandResult("cmd", 0, service_status_json("active"), ""))

    assert state.ok is True
    assert state.state == "active"
    assert state.summary() == "active"


def test_parse_service_status_failed_keeps_the_reason() -> None:
    document = service_status_json("failed", detail="ActiveState=failed\nSubState=failed")
    state = parse_service_status(CommandResult("cmd", 0, document, ""))

    assert state.ok is False
    assert state.summary() == "failed: ActiveState=failed"


def test_parse_service_status_unparseable_output_is_not_success() -> None:
    state = parse_service_status(CommandResult("cmd", 0, "worker service: running", ""))

    assert state.ok is False
    assert state.state == "unknown"
    assert state.summary() == "unknown: worker service: running"


def test_confirmed_service_status_rechecks_an_active_service() -> None:
    runner = FakeSshRunner()
    sleeps: list[float] = []

    state = confirmed_service_status(
        runner, venv_dir=VENV_DIR, config_path=CONFIG_PATH, sleep=sleeps.append
    )

    assert state.ok is True
    assert sleeps == [STATUS_SETTLE_SECONDS]
    assert len(runner.commands_with("service-status")) == 2


def test_confirmed_service_status_catches_a_service_that_dies() -> None:
    runner = FakeSshRunner()
    answers = iter(
        [
            service_status_json("active"),
            service_status_json("failed", detail="ActiveState=failed"),
        ]
    )
    runner.on("service-status", lambda command: CommandResult(command, 0, next(answers), ""))
    sleeps: list[float] = []

    state = confirmed_service_status(
        runner, venv_dir=VENV_DIR, config_path=CONFIG_PATH, sleep=sleeps.append
    )

    assert state.ok is False
    assert state.summary() == "failed: ActiveState=failed"
    assert sleeps == [STATUS_SETTLE_SECONDS]


def test_confirmed_service_status_returns_a_non_active_state_immediately() -> None:
    runner = FakeSshRunner(service_out=service_status_json("inactive"))
    sleeps: list[float] = []

    state = confirmed_service_status(
        runner, venv_dir=VENV_DIR, config_path=CONFIG_PATH, sleep=sleeps.append
    )

    assert state.ok is False
    assert sleeps == []
    assert len(runner.commands_with("service-status")) == 1


def test_uninstall_service_failure_raises() -> None:
    runner = FakeSshRunner().on("uninstall-service", 1)
    with pytest.raises(ServiceError, match="uninstall-service failed"):
        uninstall_service(runner, venv_dir=VENV_DIR, config_path=CONFIG_PATH)
