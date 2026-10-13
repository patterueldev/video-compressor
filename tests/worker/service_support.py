"""Shared factories for the worker service suites (issue #51).

No real service manager is ever touched: tests inject :class:`RecordingRunner`
and a :class:`ServicePaths` rooted in ``tmp_path``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from videocompressor.worker.service import CommandResult, ServicePaths, ServiceSpec

TOKEN = "secret-token"
WORKER_ID = "node-a"

#: Pinned PATH a rendered unit carries in tests; production detection is in
#: ``videocompressor.worker.toolpaths`` (issue #71).
PATH_VALUE = "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

COMMAND = (
    "/opt/venv/bin/python",
    "-m",
    "videocompressor.cli",
    "worker",
    "--config",
    "/abs/worker.toml",
)


@dataclass
class RecordingRunner:
    """Command runner double: records argv and serves canned results."""

    by_binary: dict[str, CommandResult] = field(default_factory=dict)
    default: CommandResult = field(default_factory=lambda: CommandResult(0))
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def __call__(self, argv: Sequence[str]) -> CommandResult:
        self.calls.append(tuple(argv))
        return self.by_binary.get(argv[0], self.default)


class MissingBinaryRunner:
    """Command runner double that fails like a host without the service manager."""

    def __call__(self, argv: Sequence[str]) -> CommandResult:
        raise FileNotFoundError(argv[0])


def make_paths(tmp_path: Path, platform: str) -> ServicePaths:
    """Build injectable service locations rooted in ``tmp_path``."""
    home = tmp_path / "home"
    return ServicePaths(
        platform=platform,
        home=home,
        config_home=tmp_path / "config",
        log_dir=home / "Library" / "Logs" / "VideoCompressor",
        uid=501,
    )


def make_spec(
    config_path: Path,
    *,
    worker_id: str = WORKER_ID,
    token_env: str | None = None,
    token_value: str | None = None,
    command: tuple[str, ...] = COMMAND,
    path_value: str = PATH_VALUE,
) -> ServiceSpec:
    """Build a service spec without loading a real config file."""
    return ServiceSpec(
        worker_id=worker_id,
        config_path=config_path.resolve(),
        command=command,
        path_value=path_value,
        token_env=token_env,
        token_value=token_value,
    )


def write_worker_config(
    tmp_path: Path,
    *,
    worker_id: str = WORKER_ID,
    token: str = TOKEN,
    token_env: str | None = None,
) -> Path:
    """Write a valid per-node worker config and return its path."""
    archive = tmp_path / "archive"
    archive.mkdir(exist_ok=True)
    lines = [
        "[worker]",
        f'id = "{worker_id}"',
        'controller_url = "http://controller-host:8765"',
        f'scratch_dir = "{tmp_path / "scratch"}"',
        f'token_env = "{token_env}"' if token_env else f'token = "{token}"',
        "",
        "[roots]",
        f'archive = "{archive}"',
        "",
    ]
    path = tmp_path / "worker.toml"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def systemd_show(
    *,
    active: str = "inactive",
    sub: str = "dead",
    unit_file: str = "enabled",
    exec_main: str = "0",
) -> CommandResult:
    """Canned ``systemctl --user show`` output."""
    text = (
        f"ActiveState={active}\n"
        f"SubState={sub}\n"
        f"UnitFileState={unit_file}\n"
        f"ExecMainStatus={exec_main}\n"
    )
    return CommandResult(0, text, "")


def probe(platform: str, *, active: bool) -> dict[str, CommandResult]:
    """Canned read-only probe results for one platform."""
    if platform == "macos":
        state = "running" if active else "not running"
        return {"launchctl": CommandResult(0, f"state = {state}\n", "")}
    return {"systemctl": systemd_show(active="active" if active else "inactive")}
