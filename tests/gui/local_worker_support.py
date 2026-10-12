"""Socket- and process-free fakes for the local-worker suites (issue #52).

No Qt imports here on purpose: the pure-Python suites use these helpers and keep
running without the optional ``gui`` extra.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from videocompressor.core.config import AppConfig, register_root
from videocompressor.gui.local_worker_service import ServiceCommandResult

__all__ = [
    "FakeProcess",
    "FakeRegistry",
    "FakeServiceRunner",
    "FakeSpawner",
    "StubbornProcess",
    "make_config",
    "roots_payload",
    "worker_entry",
]


def make_config(
    tmp_path: Path,
    *,
    enabled: bool = True,
    keep_running: bool = False,
    token: str | None = "test-token",
    host: str = "127.0.0.1",
    port: int = 8765,
    scratch_dir: Path | None = None,
    thread_cap: int = 2,
    roots: bool = True,
) -> AppConfig:
    """An app config with one registered root under ``tmp_path``."""
    config = AppConfig()
    if roots:
        root_path = tmp_path / "archive"
        root_path.mkdir(parents=True, exist_ok=True)
        register_root(config, root_id="archive", name="Archive", path=root_path)
    config.controller.host = host
    config.controller.port = port
    config.controller.token = token
    config.local_worker.enabled = enabled
    config.local_worker.keep_running = keep_running
    config.local_worker.scratch_dir = scratch_dir
    config.local_worker.thread_cap = thread_cap
    return config


def roots_payload(config: AppConfig) -> dict[str, str]:
    """The worker-reported mapping for ``config`` (paths resolved like the worker's)."""
    return {root.id: str(root.path.expanduser().resolve()) for root in config.roots}


def worker_entry(
    worker_id: str = "local",
    *,
    online: bool = True,
    roots: Mapping[str, str] | None = None,
    last_seen: datetime | None = None,
) -> dict[str, Any]:
    """One ``GET /workers`` entry."""
    when = last_seen if last_seen is not None else datetime.now(UTC) - timedelta(seconds=2)
    return {
        "worker_id": worker_id,
        "name": worker_id,
        "online": online,
        "last_seen": when.isoformat(),
        "roots": dict(roots or {}),
    }


class FakeRegistry:
    """Socket-free stand-in for ``HttpControllerClient.workers``."""

    def __init__(
        self, workers: Sequence[Mapping[str, Any]] = (), *, error: Exception | None = None
    ) -> None:
        self._workers = list(workers)
        self.error = error
        self.calls = 0

    def workers(self) -> dict[str, Any]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return {"workers": list(self._workers)}


class FakeProcess:
    """A process that exits cleanly on terminate."""

    def __init__(self) -> None:
        self.returncode: int | None = None
        self.terminate_calls = 0
        self.kill_calls = 0
        self.waits: list[float | None] = []

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminate_calls += 1
        self.returncode = 0

    def kill(self) -> None:
        self.kill_calls += 1
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        self.waits.append(timeout)
        return self.returncode if self.returncode is not None else 0


class StubbornProcess(FakeProcess):
    """Ignores SIGTERM: the first wait times out and ``kill()`` is required."""

    def terminate(self) -> None:
        self.terminate_calls += 1

    def wait(self, timeout: float | None = None) -> int:
        self.waits.append(timeout)
        if self.returncode is None:
            raise subprocess.TimeoutExpired(cmd="worker", timeout=timeout or 0.0)
        return self.returncode

    def kill(self) -> None:
        self.kill_calls += 1
        self.returncode = -9


class FakeSpawner:
    """Records spawn argv/env/log dir and returns the configured process."""

    def __init__(self, process: FakeProcess | None = None) -> None:
        self.process = process if process is not None else FakeProcess()
        self.calls: list[tuple[tuple[str, ...], dict[str, str], Path]] = []

    def __call__(
        self, argv: Sequence[str], env: Mapping[str, str], log_dir: Path
    ) -> FakeProcess:
        self.calls.append((tuple(argv), dict(env), Path(log_dir)))
        return self.process


class FakeServiceRunner:
    """Canned schema-1 JSON per operation; records the argv and env it saw."""

    def __init__(
        self,
        *,
        installed: bool = False,
        state: str = "not_installed",
        install_result: str = "started",
        uninstall_result: str = "removed",
        failing: set[str] | frozenset[str] = frozenset(),
    ) -> None:
        self.installed = installed
        self.state = state
        self.install_result = install_result
        self.uninstall_result = uninstall_result
        self.failing = set(failing)
        self.calls: list[tuple[tuple[str, ...], dict[str, str]]] = []

    def __call__(self, argv: Sequence[str], env: Mapping[str, str]) -> ServiceCommandResult:
        argv = tuple(argv)
        self.calls.append((argv, dict(env)))
        operation = argv[-2]
        if operation in self.failing:
            return ServiceCommandResult(1, "", f"{operation} failed")
        if operation == "service-status":
            payload = {"installed": self.installed, "state": self.state}
        elif operation == "install-service":
            payload = {"result": self.install_result}
            self.installed = True
        else:
            payload = {"result": self.uninstall_result}
            self.installed = False
        return ServiceCommandResult(0, json.dumps(payload), "")

    @property
    def operations(self) -> list[str]:
        return [argv[-2] for argv, _ in self.calls]
