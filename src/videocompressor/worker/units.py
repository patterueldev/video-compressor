"""Launchd/systemd unit rendering for the worker service (issue #51).

Pure helpers only: names, target paths, and the platform unit documents. Nothing
here runs a command or writes a file; :mod:`videocompressor.worker.service` owns
those side effects.

Frozen naming and locations (see ``docs/worker-runbook.md``)::

    macOS  ~/Library/LaunchAgents/com.videocompressor.worker.<id>.plist
    Linux  $XDG_CONFIG_HOME/systemd/user/videocompressor-worker-<id>.service
    env    $XDG_CONFIG_HOME/videocompressor/worker-<id>.env     (token_env only)
"""

from __future__ import annotations

import plistlib
import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from videocompressor.worker.service import ServicePaths, ServiceSpec

__all__ = [
    "DEFAULT_RESTART_SECONDS",
    "LAUNCHD_LABEL_PREFIX",
    "REDACTED",
    "SYSTEMD_UNIT_PREFIX",
    "env_file_path",
    "launchd_label",
    "log_paths",
    "render_launchd_plist",
    "render_systemd_unit",
    "render_unit",
    "systemd_unit_name",
    "unit_path",
]

LAUNCHD_LABEL_PREFIX = "com.videocompressor.worker."
SYSTEMD_UNIT_PREFIX = "videocompressor-worker-"

#: Restart delay after a failed run (launchd ``ThrottleInterval``, systemd ``RestartSec``).
DEFAULT_RESTART_SECONDS = 30

#: Marker shown instead of a token value in a ``--dry-run`` preview.
REDACTED = "<redacted>"

_SYSTEMD_SAFE_ARG = re.compile(r"[A-Za-z0-9_@%+=:,./-]+")


def launchd_label(worker_id: str) -> str:
    """Reverse-DNS launchd label for ``worker_id``."""
    return f"{LAUNCHD_LABEL_PREFIX}{worker_id}"


def systemd_unit_name(worker_id: str) -> str:
    """systemd user-unit file name for ``worker_id``."""
    return f"{SYSTEMD_UNIT_PREFIX}{worker_id}.service"


def unit_path(spec: ServiceSpec, paths: ServicePaths) -> Path:
    """Target path of the platform unit for this config."""
    if paths.platform == "macos":
        return paths.home / "Library" / "LaunchAgents" / f"{launchd_label(spec.worker_id)}.plist"
    return paths.config_home / "systemd" / "user" / systemd_unit_name(spec.worker_id)


def env_file_path(spec: ServiceSpec, paths: ServicePaths) -> Path | None:
    """File that carries a ``token_env`` token, or ``None`` when unused.

    macOS embeds the variable in the plist; only systemd needs a separate
    ``EnvironmentFile``.
    """
    if spec.token_env is None or paths.platform == "macos":
        return None
    return paths.config_home / "videocompressor" / f"worker-{spec.worker_id}.env"


def log_paths(spec: ServiceSpec, paths: ServicePaths) -> dict[str, str]:
    """Log destination for the unit: launchd files or the systemd journal."""
    if paths.platform == "macos":
        return {
            "stdout": str(paths.log_dir / f"{spec.worker_id}.out.log"),
            "stderr": str(paths.log_dir / f"{spec.worker_id}.err.log"),
        }
    return {"journal": systemd_unit_name(spec.worker_id)}


def render_unit(
    spec: ServiceSpec, paths: ServicePaths, *, redact_token: bool = False
) -> bytes | str:
    """Render the platform unit document (``bytes`` for the plist)."""
    if paths.platform == "macos":
        return render_launchd_plist(spec, paths, redact_token=redact_token)
    return render_systemd_unit(spec, paths, redact_token=redact_token)


def render_launchd_plist(
    spec: ServiceSpec, paths: ServicePaths, *, redact_token: bool = False
) -> bytes:
    """Render the LaunchAgent plist (deterministic XML, sorted keys)."""
    payload: dict[str, object] = {
        "Label": launchd_label(spec.worker_id),
        "ProgramArguments": list(spec.command),
        "WorkingDirectory": str(spec.config_path.parent),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": DEFAULT_RESTART_SECONDS,
        "ProcessType": "Background",
        "StandardOutPath": str(paths.log_dir / f"{spec.worker_id}.out.log"),
        "StandardErrorPath": str(paths.log_dir / f"{spec.worker_id}.err.log"),
    }
    if spec.token_env is not None:
        value = REDACTED if redact_token else (spec.token_value or "")
        payload["EnvironmentVariables"] = {spec.token_env: value}
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)


def render_systemd_unit(
    spec: ServiceSpec, paths: ServicePaths, *, redact_token: bool = False
) -> str:
    """Render the systemd user unit (the token value never appears in it)."""
    del redact_token  # only the EnvironmentFile path is rendered, never the value
    lines = [
        "[Unit]",
        f"Description=Video Compressor worker {_escape(spec.worker_id)}",
        "After=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        f"WorkingDirectory={_arg(str(spec.config_path.parent))}",
        f"ExecStart={' '.join(_arg(part) for part in spec.command)}",
    ]
    env_file = env_file_path(spec, paths)
    if env_file is not None:
        lines.append(f"EnvironmentFile={_arg(str(env_file))}")
    lines += [
        "Restart=on-failure",
        f"RestartSec={DEFAULT_RESTART_SECONDS}",
        "StandardOutput=journal",
        "StandardError=journal",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return "\n".join(lines)


def _escape(value: str) -> str:
    """Double ``%`` as systemd expands specifiers in every unit value."""
    return value.replace("%", "%%")


def _arg(value: str) -> str:
    """Quote one systemd argument or path when it needs it."""
    escaped = _escape(value)
    if _SYSTEMD_SAFE_ARG.fullmatch(escaped):
        return escaped
    escaped = escaped.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
