"""Install, query, and remove the per-user worker service (issue #51).

The service runs the same foreground agent (``vc worker --config PATH``); this
module owns the launchd (macOS LaunchAgent) and systemd (Linux user unit)
side effects. Everything observable is injectable — :class:`ServicePaths` for the
filesystem locations, :class:`CommandRunner` for the platform commands — so the
test suites never touch a real service manager. See ``docs/worker-runbook.md``.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from videocompressor.worker.config import WorkerConfig
from videocompressor.worker.service_platform import (
    CommandResult,
    CommandRunner,
    ServiceError,
    bring_up_actions,
    check_platform,
    default_runner,
    launchd_print_argv,
    reload_actions,
    run,
    run_actions,
    start_actions,
    stop_actions,
    systemd_show_argv,
)
from videocompressor.worker.toolpaths import service_path_value
from videocompressor.worker.units import (
    env_file_path,
    log_paths,
    render_unit,
    unit_path,
)

__all__ = [
    "CommandResult",
    "CommandRunner",
    "ServiceError",
    "ServicePaths",
    "ServiceResult",
    "ServiceSpec",
    "ServiceStatus",
    "default_paths",
    "default_runner",
    "default_worker_command",
    "install_service",
    "platform_name",
    "service_status",
    "spec_for_worker_id",
    "spec_from_config",
    "uninstall_service",
]

PLATFORM_MACOS = "macos"
PLATFORM_LINUX = "linux"
SCOPE_LAUNCH_AGENT = "launch_agent"
SCOPE_SYSTEMD_USER = "systemd_user"

STATE_NOT_INSTALLED = "not_installed"
STATE_ACTIVE = "active"
STATE_INACTIVE = "inactive"
STATE_FAILED = "failed"

RESULT_INSTALLED = "installed"
RESULT_UPDATED = "updated"
RESULT_UNCHANGED = "unchanged"
RESULT_STARTED = "started"
RESULT_REMOVED = "removed"
RESULT_ABSENT = "absent"

_STATE_BY_SYSTEMD = {STATE_ACTIVE: STATE_ACTIVE, STATE_FAILED: STATE_FAILED}

_JSON_SCHEMA = 1
_UNIT_MODE_MACOS = 0o600  # the plist may embed the token
_UNIT_MODE_LINUX = 0o644
_SECRET_MODE = 0o600


@dataclass(frozen=True)
class ServicePaths:
    """Filesystem and runtime locations the service operations use.

    :func:`default_paths` derives them for the current user; tests and the
    library API (#52/#53) inject their own.
    """

    platform: str
    home: Path
    config_home: Path
    log_dir: Path
    uid: int


def platform_name() -> str:
    """Name the platform's service manager, or fail for unsupported systems."""
    if sys.platform == "darwin":
        return PLATFORM_MACOS
    if sys.platform.startswith("linux"):
        return PLATFORM_LINUX
    raise ServiceError("worker services are supported on macOS (launchd) and Linux (systemd) only")


def default_paths(platform: str | None = None) -> ServicePaths:
    """Build the service locations for the current user."""
    resolved = platform or platform_name()
    home = Path.home()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    config_home = Path(xdg) if xdg and Path(xdg).is_absolute() else home / ".config"
    return ServicePaths(
        platform=resolved,
        home=home,
        config_home=config_home,
        log_dir=home / "Library" / "Logs" / "VideoCompressor",
        uid=os.getuid(),
    )


@dataclass(frozen=True)
class ServiceSpec:
    """The service a worker config asks for: identity, command, PATH, token source."""

    worker_id: str
    config_path: Path
    command: tuple[str, ...]
    #: PATH the rendered unit runs with: the detected ffmpeg/ffprobe directories
    #: plus the standard system paths (issue #71).
    path_value: str
    token_env: str | None = None
    token_value: str | None = None


def default_worker_command(config_path: Path) -> tuple[str, ...]:
    """Absolute argv the service runs: this interpreter, the CLI module, the config.

    ``sys.executable`` is used unresolved on purpose: resolving a venv's python
    symlink escapes the environment, so the unit could no longer import the
    package installed in the venv. #52's ``worker_command`` follows the same rule.
    """
    return (
        sys.executable,
        "-m",
        "videocompressor.cli",
        "worker",
        "--config",
        str(config_path),
    )


def spec_from_config(
    config: WorkerConfig, config_path: Path, *, command: Sequence[str] | None = None
) -> ServiceSpec:
    """Build a install-time spec from a fully validated worker config.

    The token value is carried only so a ``token_env`` config can be materialized
    into the unit environment; it is never rendered into output, and the plist /
    env file are written mode 0600.
    """
    return _spec(
        config.worker_id,
        config_path,
        command=command,
        token_env=config.token_env,
        token_value=config.token if config.token_env else None,
    )


def spec_for_worker_id(
    worker_id: str, config_path: Path, *, command: Sequence[str] | None = None
) -> ServiceSpec:
    """Build a status/uninstall spec from a light config read (id only)."""
    return _spec(worker_id, config_path, command=command)


def _spec(
    worker_id: str,
    config_path: Path,
    *,
    command: Sequence[str] | None,
    token_env: str | None = None,
    token_value: str | None = None,
) -> ServiceSpec:
    resolved = config_path.resolve()
    return ServiceSpec(
        worker_id=worker_id,
        config_path=resolved,
        command=tuple(command) if command is not None else default_worker_command(resolved),
        path_value=service_path_value(),
        token_env=token_env,
        token_value=token_value,
    )


@dataclass(frozen=True)
class ServiceResult:
    """Outcome of an install or uninstall operation."""

    operation: str
    result: str
    spec: ServiceSpec
    paths: ServicePaths
    actions: tuple[tuple[str, ...], ...] = ()
    dry_run: bool = False

    def to_dict(self) -> dict[str, object]:
        """The frozen ``--json`` payload (schema 1)."""
        payload = _common_payload(self.spec, self.paths)
        payload.update(
            {
                "operation": self.operation,
                "dry_run": self.dry_run,
                "result": self.result,
                "actions": [list(argv) for argv in self.actions],
            }
        )
        return payload


@dataclass(frozen=True)
class ServiceStatus:
    """Reported state of one worker service."""

    spec: ServiceSpec
    paths: ServicePaths
    installed: bool
    state: str
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        """The frozen ``--json`` payload (schema 1)."""
        payload = _common_payload(self.spec, self.paths)
        payload.update(
            {
                "operation": "service-status",
                "installed": self.installed,
                "state": self.state,
                "detail": self.detail,
            }
        )
        return payload


def install_service(
    spec: ServiceSpec, *, paths: ServicePaths, runner: CommandRunner, dry_run: bool = False
) -> ServiceResult:
    """Create or refresh the unit and make sure the service is running.

    Idempotent: an identical unit on an active service reports ``unchanged`` and
    runs no command; a changed unit is rewritten and the service restarted.
    """
    check_platform(paths)
    unit = unit_path(spec, paths)
    desired_unit = _document_bytes(render_unit(spec, paths))
    desired_env = _env_document(spec)
    env_file = env_file_path(spec, paths)

    installed = unit.is_file()
    unchanged = installed and unit.read_bytes() == desired_unit
    if unchanged and spec.token_env is not None and env_file is not None:
        unchanged = env_file.is_file() and env_file.read_bytes() == desired_env

    if unchanged:
        active = service_status(spec, paths=paths, runner=runner).state == STATE_ACTIVE
        result = RESULT_UNCHANGED if active else RESULT_STARTED
        actions = () if active else start_actions(spec, paths)
    else:
        result = RESULT_UPDATED if installed else RESULT_INSTALLED
        actions = bring_up_actions(spec, paths, update=installed)
        if not dry_run:
            _write_file(unit, desired_unit, _unit_mode(paths))
            if spec.token_env is not None and env_file is not None:
                _write_file(env_file, desired_env, _SECRET_MODE)

    if not dry_run and paths.platform == PLATFORM_MACOS:
        # launchd fails to bootstrap when a StandardOutPath/StandardErrorPath
        # directory is missing, so create it before the agent is loaded.
        paths.log_dir.mkdir(parents=True, exist_ok=True)

    executed = run_actions(actions, runner, dry_run=dry_run)
    return ServiceResult("install-service", result, spec, paths, executed, dry_run)


def uninstall_service(
    spec: ServiceSpec, *, paths: ServicePaths, runner: CommandRunner, dry_run: bool = False
) -> ServiceResult:
    """Stop the service and remove its unit (idempotent: ``absent`` when gone)."""
    check_platform(paths)
    unit = unit_path(spec, paths)
    if not unit.is_file():
        # Clean a leftover token env file even when the unit itself is gone;
        # uninstall must never leave a plaintext secret behind.
        if not dry_run:
            _remove_env_file(spec, paths)
        return ServiceResult("uninstall-service", RESULT_ABSENT, spec, paths, (), dry_run)

    executed = run_actions(stop_actions(spec, paths), runner, dry_run=dry_run)
    if not dry_run:
        unit.unlink(missing_ok=True)
        _remove_env_file(spec, paths)
    executed += run_actions(reload_actions(spec, paths), runner, dry_run=dry_run)
    return ServiceResult("uninstall-service", RESULT_REMOVED, spec, paths, executed, dry_run)


def service_status(
    spec: ServiceSpec, *, paths: ServicePaths, runner: CommandRunner
) -> ServiceStatus:
    """Report whether the unit is installed and what the service manager says.

    ``not_installed`` never probes the service manager; a failed probe (for
    example a unit that was never loaded) reports ``inactive`` with the raw
    platform output in ``detail``.
    """
    check_platform(paths)
    unit = unit_path(spec, paths)
    if not unit.is_file():
        return ServiceStatus(spec, paths, False, STATE_NOT_INSTALLED)

    argv = (
        launchd_print_argv(spec, paths)
        if paths.platform == PLATFORM_MACOS
        else systemd_show_argv(spec)
    )
    result = run(argv, runner, ignore_failure=True)
    if paths.platform == PLATFORM_MACOS:
        if result.returncode == 0 and "state = running" in result.stdout:
            return ServiceStatus(spec, paths, True, STATE_ACTIVE, result.stdout.strip())
        detail = (result.stderr or result.stdout).strip()
        return ServiceStatus(spec, paths, True, STATE_INACTIVE, detail)

    state = _STATE_BY_SYSTEMD.get(_systemd_property(result.stdout, "ActiveState"), STATE_INACTIVE)
    return ServiceStatus(spec, paths, True, state, result.stdout.strip())


def _systemd_property(output: str, key: str) -> str:
    prefix = f"{key}="
    for line in output.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    return ""


# -- files -------------------------------------------------------------------


def _write_file(target: Path, content: bytes, mode: int) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.name}.tmp")
    temporary.write_bytes(content)
    temporary.chmod(mode)
    temporary.replace(target)


def _document_bytes(document: bytes | str) -> bytes:
    return document if isinstance(document, bytes) else document.encode()


def _env_document(spec: ServiceSpec) -> bytes:
    if spec.token_env is None:
        return b""
    return f"{spec.token_env}={spec.token_value or ''}\n".encode()


def _remove_env_file(spec: ServiceSpec, paths: ServicePaths) -> None:
    env_file = env_file_path(spec, paths)
    if env_file is not None:
        env_file.unlink(missing_ok=True)


def _unit_mode(paths: ServicePaths) -> int:
    return _UNIT_MODE_MACOS if paths.platform == PLATFORM_MACOS else _UNIT_MODE_LINUX


def _common_payload(spec: ServiceSpec, paths: ServicePaths) -> dict[str, object]:
    env_file = env_file_path(spec, paths)
    return {
        "schema": _JSON_SCHEMA,
        "worker_id": spec.worker_id,
        "config": str(spec.config_path),
        "platform": paths.platform,
        "scope": SCOPE_LAUNCH_AGENT if paths.platform == PLATFORM_MACOS else SCOPE_SYSTEMD_USER,
        "unit": str(unit_path(spec, paths)),
        "env_file": str(env_file) if env_file is not None else None,
        "logs": log_paths(spec, paths),
    }
