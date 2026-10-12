"""Platform plumbing for the worker service (issue #51).

Command results, the runner seam, and the launchd/systemd argv tables used by
:mod:`videocompressor.worker.service`. Keeping the platform commands here holds
the operations module inside the size budget and keeps both files pure enough to
test with an injected runner (no real service manager in CI).
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from videocompressor.worker.units import launchd_label, systemd_unit_name, unit_path

if TYPE_CHECKING:
    from videocompressor.worker.service import ServicePaths, ServiceSpec

__all__ = [
    "CommandResult",
    "CommandRunner",
    "ServiceError",
    "bring_up_actions",
    "check_platform",
    "default_runner",
    "launchd_print_argv",
    "reload_actions",
    "run",
    "run_actions",
    "start_actions",
    "stop_actions",
    "systemd_show_argv",
]

PLATFORM_MACOS = "macos"
PLATFORM_LINUX = "linux"


class ServiceError(RuntimeError):
    """A service operation could not be completed on this platform."""


@dataclass(frozen=True)
class CommandResult:
    """Captured output of one platform command."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


class CommandRunner(Protocol):
    """Seam for running platform commands (tests inject a recording double)."""

    def __call__(self, argv: Sequence[str]) -> CommandResult: ...


def default_runner(argv: Sequence[str]) -> CommandResult:
    """Run one platform command and capture its output."""
    completed = subprocess.run(list(argv), capture_output=True, text=True, check=False)
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


@dataclass(frozen=True)
class _Action:
    """A platform command with its failure policy."""

    argv: tuple[str, ...]
    ignore_failure: bool = False


def run(argv: tuple[str, ...], runner: CommandRunner, *, ignore_failure: bool) -> CommandResult:
    """Run one command, mapping a missing binary and failures to ``ServiceError``."""
    try:
        result = runner(argv)
    except FileNotFoundError as exc:
        raise ServiceError(f"{argv[0]} is not available on this machine") from exc
    if result.returncode != 0 and not ignore_failure:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise ServiceError(f"{' '.join(argv)} failed: {detail}")
    return result


def run_actions(
    actions: tuple[_Action, ...], runner: CommandRunner, *, dry_run: bool
) -> tuple[tuple[str, ...], ...]:
    """Run (or, on a dry run, only report) planned commands in order."""
    if not dry_run:
        for action in actions:
            run(action.argv, runner, ignore_failure=action.ignore_failure)
    return tuple(action.argv for action in actions)


def bring_up_actions(
    spec: ServiceSpec, paths: ServicePaths, *, update: bool
) -> tuple[_Action, ...]:
    """Load/enable the service, restarting the agent when the unit changed."""
    if paths.platform == PLATFORM_MACOS:
        # ``bootout`` unloads a previous instance (failure means "not loaded" and
        # is ignored); ``bootstrap`` then loads and starts the agent.
        return (
            _Action(("launchctl", "bootout", launchd_target(spec, paths)), True),
            _Action(("launchctl", "bootstrap", f"gui/{paths.uid}", str(unit_path(spec, paths)))),
        )
    actions = [_Action(("systemctl", "--user", "daemon-reload"))]
    unit = systemd_unit_name(spec.worker_id)
    if update:
        actions.append(_Action(("systemctl", "--user", "restart", unit)))
    else:
        actions.append(_Action(("systemctl", "--user", "enable", "--now", unit)))
    return tuple(actions)


def start_actions(spec: ServiceSpec, paths: ServicePaths) -> tuple[_Action, ...]:
    """Bring an installed but inactive service up."""
    return bring_up_actions(spec, paths, update=False)


def stop_actions(spec: ServiceSpec, paths: ServicePaths) -> tuple[_Action, ...]:
    """Stop and disable the service before its unit is removed."""
    if paths.platform == PLATFORM_MACOS:
        return (_Action(("launchctl", "bootout", launchd_target(spec, paths)), True),)
    unit = systemd_unit_name(spec.worker_id)
    return (_Action(("systemctl", "--user", "disable", "--now", unit)),)


def reload_actions(spec: ServiceSpec, paths: ServicePaths) -> tuple[_Action, ...]:
    """Refresh the manager state after a unit file was removed."""
    if paths.platform == PLATFORM_MACOS:
        return ()
    return (_Action(("systemctl", "--user", "daemon-reload")),)


def launchd_print_argv(spec: ServiceSpec, paths: ServicePaths) -> tuple[str, ...]:
    """Read-only launchd probe of the agent's state."""
    return ("launchctl", "print", launchd_target(spec, paths))


def systemd_show_argv(spec: ServiceSpec) -> tuple[str, ...]:
    """Read-only systemd probe of the unit's state."""
    return (
        "systemctl",
        "--user",
        "show",
        "--no-pager",
        "--property=ActiveState",
        "--property=SubState",
        "--property=UnitFileState",
        "--property=ExecMainStatus",
        "--",
        systemd_unit_name(spec.worker_id),
    )


def launchd_target(spec: ServiceSpec, paths: ServicePaths) -> str:
    """launchd domain/label target for the agent."""
    return f"gui/{paths.uid}/{launchd_label(spec.worker_id)}"


def check_platform(paths: ServicePaths) -> None:
    """Reject injected platform values this module cannot manage."""
    if paths.platform not in (PLATFORM_MACOS, PLATFORM_LINUX):
        raise ServiceError(f"unsupported platform {paths.platform!r}")
