"""Per-node worker configuration (issue #6).

A worker node owns this TOML file: the controller URL and token, its archive-root
mappings, the scratch directory, and the limits for this machine. It is
deliberately separate from the per-user config in ``core/config.py`` (owned by
issue #8); the controller process never reads it.

Example (values are placeholders)::

    [worker]
    id = "node-a"
    name = "Processing node A"
    controller_url = "http://controller-host:8765"
    token_env = "VC_WORKER_TOKEN"     # or token = "..." (never committed)
    scratch_dir = "/path/to/scratch"
    thread_cap = 2
    poll_seconds = 5

    [roots]
    archive = "/path/to/archive"      # one entry per controller root_id

    [pilot]                           # optional: enables pilot-output mode
    root = "/path/to/pilot-output"
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

__all__ = ["MappingError", "WorkerConfig", "WorkerConfigError", "load_worker_config"]

#: Worker ids match this pattern in the controller contract (docs/architecture.md).
WORKER_ID_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,64}")

DEFAULT_THREAD_CAP = 2
DEFAULT_POLL_SECONDS = 5.0


class WorkerConfigError(ValueError):
    """The per-node worker configuration is missing or invalid."""


class MappingError(WorkerConfigError):
    """A claimed job's root or relative path cannot be resolved on this node."""


@dataclass(frozen=True)
class WorkerConfig:
    """Validated per-node worker settings."""

    worker_id: str
    controller_url: str
    token: str
    roots: Mapping[str, Path]
    scratch_dir: Path
    name: str | None = None
    thread_cap: int = DEFAULT_THREAD_CAP
    poll_seconds: float = DEFAULT_POLL_SECONDS
    pilot_root: Path | None = None
    heartbeat_seconds: float | None = None

    @property
    def pilot(self) -> bool:
        """Whether pilot-output mode is enabled for this node."""
        return self.pilot_root is not None

    def resolve(self, root_id: str, relative_path: str) -> Path:
        """Resolve one archive-relative POSIX path under its configured root.

        Raises :class:`MappingError` for an unknown root, an unsafe relative
        path, or a candidate that escapes the root.
        """
        root = self.roots.get(root_id)
        if root is None:
            raise MappingError(f"root_id {root_id!r} is not mapped on this node")
        relative = PurePosixPath(relative_path)
        if relative.is_absolute() or not relative.parts:
            raise MappingError(f"relative path {relative_path!r} is not archive-relative")
        if any(part in ("", ".", "..") for part in relative.parts):
            raise MappingError(f"relative path {relative_path!r} is not safe")
        candidate = root.joinpath(*relative.parts)
        root_resolved = root.resolve()
        if not candidate.resolve().is_relative_to(root_resolved):
            raise MappingError(f"path {relative_path!r} escapes root {root}")
        return candidate


def load_worker_config(path: Path) -> WorkerConfig:
    """Load and validate the per-node worker config at ``path``."""
    if not path.is_file():
        raise WorkerConfigError(f"worker config {path} does not exist")
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise WorkerConfigError(f"worker config {path} is not valid TOML: {exc}") from exc
    except OSError as exc:
        raise WorkerConfigError(f"cannot read worker config {path}: {exc}") from exc
    return _config_from_toml(data, path)


def _config_from_toml(data: Mapping[str, Any], path: Path) -> WorkerConfig:
    worker = data.get("worker")
    if not isinstance(worker, Mapping):
        raise WorkerConfigError(f"worker config {path} has no [worker] section")

    worker_id = _require_str(worker, "id")
    if not WORKER_ID_PATTERN.fullmatch(worker_id):
        raise WorkerConfigError(
            f"worker id {worker_id!r} must match [A-Za-z0-9_.-]{{1,64}}"
        )
    controller_url = _controller_url(_require_str(worker, "controller_url"))
    token = _token(worker)
    roots = _roots(data.get("roots"))
    scratch_dir = _ensure_dir(_path(worker, "scratch_dir"), "scratch_dir")
    thread_cap = _optional_int(worker, "thread_cap", default=DEFAULT_THREAD_CAP)
    if thread_cap < 1:
        raise WorkerConfigError("thread_cap must be a positive integer")
    poll_seconds = _optional_float(worker, "poll_seconds", default=DEFAULT_POLL_SECONDS)
    if poll_seconds <= 0:
        raise WorkerConfigError("poll_seconds must be positive")
    heartbeat_seconds = _optional_float(worker, "heartbeat_seconds", default=0.0) or None
    if heartbeat_seconds is not None and heartbeat_seconds <= 0:
        raise WorkerConfigError("heartbeat_seconds must be positive")
    pilot_root = _pilot_root(data.get("pilot"))

    return WorkerConfig(
        worker_id=worker_id,
        name=_optional_str(worker, "name") or None,
        controller_url=controller_url,
        token=token,
        roots=roots,
        scratch_dir=scratch_dir,
        thread_cap=thread_cap,
        poll_seconds=poll_seconds,
        pilot_root=pilot_root,
        heartbeat_seconds=heartbeat_seconds,
    )


def _controller_url(value: str) -> str:
    split = urlsplit(value)
    if split.scheme not in ("http", "https") or not split.netloc:
        raise WorkerConfigError(f"controller_url {value!r} must be an http(s) URL")
    if split.path not in ("", "/") or split.query or split.fragment:
        raise WorkerConfigError(
            f"controller_url {value!r} must not carry a path (the client appends /api/v1)"
        )
    return value.rstrip("/")


def _token(worker: Mapping[str, Any]) -> str:
    token = _optional_str(worker, "token")
    token_env = _optional_str(worker, "token_env")
    if token is not None and token_env is not None:
        raise WorkerConfigError("set either token or token_env, not both")
    if token is not None:
        return token
    if token_env is not None:
        value = os.environ.get(token_env)
        if not value:
            raise WorkerConfigError(f"environment variable {token_env} is not set")
        return value
    raise WorkerConfigError("worker config needs a token or token_env")


def _roots(section: object) -> dict[str, Path]:
    if not isinstance(section, Mapping) or not section:
        raise WorkerConfigError("worker config needs a non-empty [roots] section")
    roots: dict[str, Path] = {}
    for root_id, value in section.items():
        if not isinstance(root_id, str) or not isinstance(value, str) or not value:
            raise WorkerConfigError("[roots] must map root ids to directory paths")
        roots[root_id] = _require_dir(_expand(value), f"roots.{root_id}")
    return roots


def _pilot_root(section: object) -> Path | None:
    if section is None:
        return None
    if not isinstance(section, Mapping):
        raise WorkerConfigError("[pilot] must be a section with a root path")
    root = section.get("root")
    if not isinstance(root, str) or not root:
        raise WorkerConfigError("[pilot] needs a root path")
    return _ensure_dir(_expand(root), "pilot.root")


def _path(section: Mapping[str, Any], key: str) -> Path:
    return _expand(_require_str(section, key))


def _expand(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def _ensure_dir(path: Path, label: str) -> Path:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise WorkerConfigError(f"{label} {path} cannot be created: {exc}") from exc
    if not path.is_dir():
        raise WorkerConfigError(f"{label} {path} is not a directory")
    return path


def _require_dir(path: Path, label: str) -> Path:
    if not path.is_dir():
        raise WorkerConfigError(f"{label} {path} is not an existing directory")
    return path


def _require_str(section: Mapping[str, Any], key: str) -> str:
    value = section.get(key)
    if not isinstance(value, str) or not value:
        raise WorkerConfigError(f"{key!r} must be a non-empty string")
    return value


def _optional_str(section: Mapping[str, Any], key: str) -> str | None:
    value = section.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise WorkerConfigError(f"{key!r} must be a string")
    return value


def _optional_int(section: Mapping[str, Any], key: str, *, default: int) -> int:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise WorkerConfigError(f"{key!r} must be an integer")
    return value


def _optional_float(section: Mapping[str, Any], key: str, *, default: float) -> float:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WorkerConfigError(f"{key!r} must be a number")
    return float(value)
