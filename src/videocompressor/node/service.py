"""Adapter for the frozen #51 remote service CLI (issue #53).

Issue #51 freezes ``vc worker install-service|service-status|uninstall-service
--config PATH`` on the worker host. Every call lives here, so a change to the
frozen surface is a one-file change. ``service-status`` runs with ``--json`` and
its schema-1 document is parsed (:func:`parse_service_status`): the exit code
only says the report was produced, so a missing or unparseable document is never
treated as success.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath

from videocompressor.node.models import CommandResult, ServiceError
from videocompressor.node.ssh import SshRunner

__all__ = [
    "SERVICE_TIMEOUT",
    "STATUS_SETTLE_SECONDS",
    "ServiceState",
    "build_service_command",
    "build_status_command",
    "confirmed_service_status",
    "install_service",
    "parse_service_status",
    "service_status",
    "uninstall_service",
]

SERVICE_TIMEOUT = 120.0

#: Pause before confirming an ``active`` service: an install-service start job
#: returns before the process has proven it survives startup.
STATUS_SETTLE_SECONDS = 2.0


@dataclass(frozen=True)
class ServiceState:
    """Interpreted ``service-status --json`` state; the exit code is not enough."""

    state: str
    ok: bool
    detail: str = ""

    def summary(self) -> str:
        """One line: the state, plus the reason when the service is not active."""
        return self.state if self.ok or not self.detail else f"{self.state}: {self.detail}"


def parse_service_status(result: CommandResult) -> ServiceState:
    """Parse the schema-1 status document; unparseable output is never success.

    ``vc worker service-status`` exits 0 even for ``inactive``/``failed`` (and
    for ``not_installed``), so only the document's ``state`` says whether the
    service runs.
    """
    payload = _json_object(result.stdout)
    if payload is None:
        detail = _first_line(result.stdout or result.stderr) or "unexpected service response"
        return ServiceState("unknown", ok=False, detail=detail)
    state = str(payload.get("state") or "unknown")
    detail = _first_line(str(payload.get("detail") or ""))
    active = payload.get("installed") is True and state == "active"
    return ServiceState(state, ok=active, detail=detail)


def confirmed_service_status(
    runner: SshRunner,
    *,
    venv_dir: str,
    config_path: str,
    sleep: Callable[[float], None],
    timeout: float = SERVICE_TIMEOUT,
) -> ServiceState:
    """Read the state and confirm an ``active`` answer after a settle pause.

    ``install-service`` returns when the start job is accepted, not when the
    process has survived, so a unit that dies on startup must be re-checked. A
    non-active answer is already the failure signal and is returned as-is.
    """
    state = parse_service_status(
        service_status(runner, venv_dir=venv_dir, config_path=config_path, timeout=timeout)
    )
    if not state.ok:
        return state
    sleep(STATUS_SETTLE_SECONDS)
    return parse_service_status(
        service_status(runner, venv_dir=venv_dir, config_path=config_path, timeout=timeout)
    )


def build_service_command(venv_dir: str, verb: str, config_path: str) -> str:
    """The exact command frozen by issue #51, paths quoted."""
    vc = str(PurePosixPath(venv_dir) / "bin" / "vc")
    return f"{shlex.quote(vc)} worker {verb} --config {shlex.quote(config_path)}"


def build_status_command(venv_dir: str, config_path: str) -> str:
    """The frozen #51 status command with the machine-readable ``--json`` flag."""
    return f"{build_service_command(venv_dir, 'service-status', config_path)} --json"


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
    """Query the service; interpret the result with :func:`parse_service_status`."""
    return runner.run(build_status_command(venv_dir, config_path), timeout=timeout)


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


def _json_object(text: str) -> dict[str, object] | None:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:160]
    return ""


def _output(result: CommandResult) -> str:
    return result.stdout or result.stderr


def _tail(result: CommandResult) -> str:
    lines = [line.strip() for line in _output(result).splitlines() if line.strip()]
    return lines[0][:200] if lines else "no output"
