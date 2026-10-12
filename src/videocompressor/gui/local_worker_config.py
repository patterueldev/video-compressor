"""Generated worker config and argv for the app-managed local worker (issue #52).

Pure Python and PySide6-free. The generated ``local-worker.toml`` maps every
registered root to this machine's path and points the worker at the controller
token through ``VC_WORKER_TOKEN`` only, so the file never carries a secret. The
argv helpers mirror issue #51's frozen surface exactly (parity-tested), because
``gui`` must not import ``videocompressor.worker``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import tomli_w

from videocompressor.core.config import AppConfig
from videocompressor.gui.controller_session import connect_host

__all__ = [
    "ERR_LOG_FILENAME",
    "OUT_LOG_FILENAME",
    "STATE_FILENAME",
    "TOKEN_ENV_VAR",
    "WORKER_CONFIG_FILENAME",
    "build_local_worker_toml",
    "default_scratch_dir",
    "local_worker_toml_path",
    "service_command",
    "worker_command",
]

#: Environment variable the generated worker config expects (never the value).
TOKEN_ENV_VAR = "VC_WORKER_TOKEN"

WORKER_CONFIG_FILENAME = "local-worker.toml"
STATE_FILENAME = "local-worker.state.json"
OUT_LOG_FILENAME = "local-worker.out.log"
ERR_LOG_FILENAME = "local-worker.err.log"


def local_worker_toml_path(config_path: Path) -> Path:
    """The generated worker config, app-owned next to ``config.toml``."""
    return config_path.parent / WORKER_CONFIG_FILENAME


def default_scratch_dir(config_path: Path) -> Path:
    """Scratch beside the app state: local disk, never a registered root."""
    return config_path.parent / "scratch"


def worker_command(worker_config_path: Path) -> tuple[str, ...]:
    """Foreground argv: this interpreter, the CLI, and the generated config.

    ``sys.executable`` is used unresolved on purpose: resolving a venv's python
    symlink escapes the environment, so the spawned interpreter could no longer
    import the package. (Issue #51's ``default_worker_command`` resolves it;
    that venv hazard is reported separately, not changed here.)
    """
    return (
        sys.executable,
        "-m",
        "videocompressor.cli",
        "worker",
        "--config",
        str(worker_config_path.resolve()),
    )


def service_command(worker_config_path: Path, operation: str) -> tuple[str, ...]:
    """The frozen #51 service CLI with ``--json`` for the app to parse."""
    return (*worker_command(worker_config_path), operation, "--json")


def build_local_worker_toml(config: AppConfig, *, worker_config_path: Path) -> bytes:
    """Render the worker config for the app-managed local node.

    Every registered root is mapped to this machine's path (sources and
    destinations alike). The controller token is referenced by environment
    variable only, so the file never carries a secret.
    """
    settings = config.local_worker
    scratch = settings.scratch_dir or default_scratch_dir(worker_config_path)
    document: dict[str, object] = {
        "worker": {
            "id": settings.worker_id,
            "name": "Local worker",
            "controller_url": (
                f"http://{connect_host(config.controller.host)}:{config.controller.port}"
            ),
            "token_env": TOKEN_ENV_VAR,
            "scratch_dir": str(scratch.expanduser()),
            "thread_cap": settings.thread_cap,
        },
        "roots": {root.id: str(root.path.expanduser()) for root in config.roots},
    }
    return tomli_w.dumps(document).encode("utf-8")
