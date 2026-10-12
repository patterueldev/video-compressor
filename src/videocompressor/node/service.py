"""Adapter for the frozen #51 remote service CLI (issue #53).

Issue #51 freezes ``vc worker install-service|service-status|uninstall-service
--config PATH`` on the worker host. Every call lives here, so a change to the
frozen surface is a one-file change. ``service-status`` exit 0 is read as
"installed / running"; any other exit is recorded as unknown / not running
(never silently treated as success).
"""

from __future__ import annotations

import shlex
from pathlib import PurePosixPath

from videocompressor.node.models import CommandResult, ServiceError
from videocompressor.node.ssh import SshRunner

__all__ = [
    "SERVICE_TIMEOUT",
    "build_service_command",
    "install_service",
    "service_status",
    "uninstall_service",
]

SERVICE_TIMEOUT = 120.0


def build_service_command(venv_dir: str, verb: str, config_path: str) -> str:
    """The exact command frozen by issue #51, paths quoted."""
    vc = str(PurePosixPath(venv_dir) / "bin" / "vc")
    return f"{shlex.quote(vc)} worker {verb} --config {shlex.quote(config_path)}"


def install_service(
    runner: SshRunner, *, venv_dir: str, config_path: str, timeout: float = SERVICE_TIMEOUT
) -> CommandResult:
    """Install and start the worker service; a non-zero exit is an error."""
    result = runner.run(
        build_service_command(venv_dir, "install-service", config_path), timeout=timeout
    )
    if result.returncode != 0:
        raise ServiceError(
            f"install-service failed (exit {result.returncode}): {_tail(result)}",
            output=_output(result),
        )
    return result


def service_status(
    runner: SshRunner, *, venv_dir: str, config_path: str, timeout: float = SERVICE_TIMEOUT
) -> CommandResult:
    """Query the service; the caller interprets the exit code and output."""
    return runner.run(
        build_service_command(venv_dir, "service-status", config_path), timeout=timeout
    )


def uninstall_service(
    runner: SshRunner, *, venv_dir: str, config_path: str, timeout: float = SERVICE_TIMEOUT
) -> CommandResult:
    """Stop and remove the worker service; a non-zero exit is an error."""
    result = runner.run(
        build_service_command(venv_dir, "uninstall-service", config_path), timeout=timeout
    )
    if result.returncode != 0:
        raise ServiceError(
            f"uninstall-service failed (exit {result.returncode}): {_tail(result)}",
            output=_output(result),
        )
    return result


def _output(result: CommandResult) -> str:
    return result.stdout or result.stderr


def _tail(result: CommandResult) -> str:
    lines = [line.strip() for line in _output(result).splitlines() if line.strip()]
    return lines[0][:200] if lines else "no output"
