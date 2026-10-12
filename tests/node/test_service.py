"""The frozen #51 service surface (issue #53)."""

from __future__ import annotations

import shlex

import pytest

from fixtures.node import CONFIG_PATH, VENV_DIR, FakeSshRunner
from videocompressor.node.models import ServiceError
from videocompressor.node.service import (
    build_service_command,
    install_service,
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


def test_uninstall_service_failure_raises() -> None:
    runner = FakeSshRunner().on("uninstall-service", 1)
    with pytest.raises(ServiceError, match="uninstall-service failed"):
        uninstall_service(runner, venv_dir=VENV_DIR, config_path=CONFIG_PATH)
