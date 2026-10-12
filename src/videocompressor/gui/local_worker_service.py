"""Subprocess seams for the app-managed local worker (issue #52).

Default implementations of the two operations the supervisor needs: spawning
the worker child (logging beside the generated config) and running the frozen
#51 service CLI. Both are injectable so the supervisor suites stay fast and
fixture-based; nothing here decides policy.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from videocompressor.gui.local_worker_config import ERR_LOG_FILENAME, OUT_LOG_FILENAME

__all__ = [
    "ProcessHandle",
    "RunServiceCommand",
    "ServiceCommandResult",
    "SpawnProcess",
    "parse_service_payload",
    "run_service_command",
    "service_error",
    "spawn_child",
]


@dataclass(frozen=True)
class ServiceCommandResult:
    """Captured result of one ``vc worker`` service command."""

    returncode: int
    stdout: str
    stderr: str


class ProcessHandle(Protocol):
    """The slice of ``subprocess.Popen`` the supervisor uses."""

    def poll(self) -> int | None: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...
    def wait(self, timeout: float | None = None) -> int: ...


SpawnProcess = Callable[[Sequence[str], Mapping[str, str], Path], ProcessHandle]
RunServiceCommand = Callable[[Sequence[str], Mapping[str, str]], ServiceCommandResult]


def spawn_child(argv: Sequence[str], env: Mapping[str, str], log_dir: Path) -> ProcessHandle:
    """Default spawner: a child logging beside its generated config.

    ``env`` is the complete child environment (the inherited process
    environment plus the token overlay) and is forwarded verbatim.

    Not a new session on purpose: the child still sees the app's terminal
    signals, and the supervisor terminates it explicitly on close/stop.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    out_path = log_dir / OUT_LOG_FILENAME
    err_path = log_dir / ERR_LOG_FILENAME
    with out_path.open("wb") as out, err_path.open("wb") as err:
        return subprocess.Popen(
            list(argv), env=dict(env), stdin=subprocess.DEVNULL, stdout=out, stderr=err
        )


def run_service_command(argv: Sequence[str], env: Mapping[str, str]) -> ServiceCommandResult:
    completed = subprocess.run(
        list(argv),
        env={**os.environ, **dict(env)},
        capture_output=True,
        text=True,
        check=False,
    )
    return ServiceCommandResult(completed.returncode, completed.stdout, completed.stderr)


def parse_service_payload(result: ServiceCommandResult) -> Mapping[str, Any] | None:
    """The schema-1 JSON document, or ``None`` for a failed/unparseable run."""
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, Mapping) else None


def service_error(result: ServiceCommandResult) -> str:
    """One token-free line for the status label."""
    line = next((line.strip() for line in result.stderr.splitlines() if line.strip()), "")
    if line:
        return line
    if result.returncode != 0:
        return f"service command failed ({result.returncode})"
    return "unexpected service response"
