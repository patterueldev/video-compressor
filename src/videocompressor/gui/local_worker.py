"""App-managed local worker supervisor (issue #52).

The app runs a worker on this machine by default (epic #50): the supervisor
generates ``local-worker.toml`` from the registered roots and starts either a
child process managed by the app (default) or the ``vc worker install-service``
service frozen by issue #51 (setting: keep running when the app closes).

Pure Python — no PySide6 — so the idempotency rules and the service decision
stay testable in CI without the optional ``gui`` extra. Subprocess plumbing
lives in :mod:`videocompressor.gui.local_worker_service`; rendering lives in
:mod:`videocompressor.gui.local_worker_config`.

Idempotency: never two local workers. A worker is only started when the
registry shows no online worker with the same id (or the same root mapping),
or when the app can prove the row is stale — ``stop()``/``shutdown()`` record
the time they confirmed no app-managed worker runs, and a row whose last
heartbeat predates that time is a dead registration, not a live node.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from videocompressor.controller.protocol import ControllerError
from videocompressor.core.config import AppConfig
from videocompressor.gui.local_worker_config import (
    ERR_LOG_FILENAME,
    STATE_FILENAME,
    TOKEN_ENV_VAR,
    build_local_worker_toml,
    default_scratch_dir,
    local_worker_toml_path,
    service_command,
    worker_command,
)
from videocompressor.gui.local_worker_service import (
    ProcessHandle,
    RunServiceCommand,
    SpawnProcess,
    parse_service_payload,
    run_service_command,
    service_error,
    spawn_child,
)
from videocompressor.gui.monitor_client import MonitorUnavailable, describe_monitor_error
from videocompressor.gui.monitor_service import parse_workers

__all__ = [
    "LocalWorkerOutcome",
    "LocalWorkerSupervisor",
    "STATE_ALREADY_RUNNING",
    "STATE_DISABLED",
    "STATE_ERROR",
    "STATE_NO_ROOTS",
    "STATE_STARTED_CHILD",
    "STATE_STARTED_SERVICE",
    "STATE_STOPPED",
    "build_local_worker_toml",
    "default_scratch_dir",
    "local_worker_toml_path",
    "service_command",
    "worker_command",
]

_log = logging.getLogger("videocompressor.gui")

STATE_DISABLED = "disabled"
STATE_NO_ROOTS = "no_roots"
STATE_STARTED_CHILD = "started_child"
STATE_STARTED_SERVICE = "started_service"
STATE_ALREADY_RUNNING = "already_running"
STATE_STOPPED = "stopped"
STATE_ERROR = "error"

#: Frozen #51 service operations used through the CLI (docs/worker-runbook.md).
INSTALL_SERVICE = "install-service"
SERVICE_STATUS = "service-status"
UNINSTALL_SERVICE = "uninstall-service"

#: Grace for a stopped child, then SIGKILL; the worker requeues on SIGTERM.
CHILD_WAIT_SECONDS = 10.0
KILL_WAIT_SECONDS = 5.0


@dataclass(frozen=True)
class LocalWorkerOutcome:
    """Result of one supervisor action, for the panel's status line."""

    state: str
    detail: str = ""

    @property
    def ok(self) -> bool:
        """Whether the action left the local worker in a usable state."""
        return self.state != STATE_ERROR


class WorkerRegistryClient(Protocol):
    """The slice of ``HttpControllerClient`` the supervisor uses."""

    def workers(self) -> Mapping[str, Any]: ...


class LocalWorkerSupervisor:
    """Start, verify, and stop the app-managed worker on this machine."""

    def __init__(
        self,
        config: AppConfig,
        config_path: Path,
        *,
        client: WorkerRegistryClient,
        spawn: SpawnProcess | None = None,
        run_command: RunServiceCommand | None = None,
    ) -> None:
        self.config = config
        self.config_path = config_path
        self._client = client
        self._spawn = spawn if spawn is not None else spawn_child
        self._run_command = run_command if run_command is not None else run_service_command
        self._process: ProcessHandle | None = None
        self._last = LocalWorkerOutcome(STATE_STOPPED, "the local worker has not been started")

    # -- public API --------------------------------------------------------

    def worker_config_path(self) -> Path:
        """Path of the generated worker config."""
        return local_worker_toml_path(self.config_path)

    def managed_child_alive(self) -> bool:
        """Whether the child started by this supervisor is still running."""
        return self._process is not None and self._process.poll() is None

    def managed_child_exit_code(self) -> int | None:
        """Exit code of the managed child, or ``None`` while it runs/never ran."""
        return self._process.poll() if self._process is not None else None

    def ensure_started(self) -> LocalWorkerOutcome:
        """Make the local worker run, unless one is already online or disabled."""
        settings = self.config.local_worker
        if not settings.enabled:
            return self._settle(STATE_DISABLED, "the local worker is off")
        if not self.config.roots:
            return self._settle(STATE_NO_ROOTS, "register an archive root first")
        if not self.config.controller.token:
            return self._settle(STATE_ERROR, "the controller token is not configured")
        if self.managed_child_alive():
            return self._settle(STATE_STARTED_CHILD, "the local worker child is running")
        try:
            self._write_worker_config()
        except OSError as error:
            return self._settle(STATE_ERROR, f"cannot write the local worker config: {error}")
        if settings.keep_running:
            return self._ensure_service()
        return self._ensure_child()

    def refresh(self) -> LocalWorkerOutcome:
        """Re-apply the current settings after roots or settings changed."""
        if not self.config.local_worker.enabled:
            return self.stop()
        try:
            changed = self._write_worker_config()
        except OSError as error:
            return self._settle(STATE_ERROR, f"cannot write the local worker config: {error}")
        if changed and self.managed_child_alive() and not self._terminate_child():
            return self._settle(
                STATE_ERROR, "the local worker child did not stop for the new configuration"
            )
        return self.ensure_started()

    def stop(self) -> LocalWorkerOutcome:
        """Stop the app-managed worker: terminate the child, remove the service."""
        if not self._terminate_child():
            return self._settle(STATE_ERROR, "the local worker child did not stop")
        details: list[str] = []
        config_path = self.worker_config_path()
        if config_path.is_file():
            result = self._run_command(service_command(config_path, UNINSTALL_SERVICE), {})
            payload = parse_service_payload(result)
            if payload is None:
                return self._settle(
                    STATE_ERROR,
                    f"cannot remove the local worker service: {service_error(result)}",
                )
            details.append(f"service {payload.get('result', 'removed')}")
        self._write_marker()
        return self._settle(STATE_STOPPED, "; ".join(["stopped", *details]))

    def shutdown(self) -> None:
        """App close: stop the managed child; leave a keep-running service up."""
        if self.config.local_worker.keep_running:
            return
        self._terminate_child()

    def status(self, *, online: bool | None = None) -> LocalWorkerOutcome:
        """Renderable state for the panel, optionally with registry knowledge."""
        if not self.config.local_worker.enabled:
            return self._settle(STATE_DISABLED, "the local worker is off")
        if not self.config.roots:
            return self._settle(STATE_NO_ROOTS, "register an archive root first")
        if self.managed_child_alive():
            return LocalWorkerOutcome(
                STATE_STARTED_CHILD,
                "online (child process)" if online else "child process running",
            )
        exit_code = self.managed_child_exit_code()
        if self._process is not None and exit_code is not None:
            return LocalWorkerOutcome(
                STATE_ERROR,
                f"the local worker child exited (code {exit_code});"
                f" see {self.config_path.parent / ERR_LOG_FILENAME}",
            )
        if online:
            if self.config.local_worker.keep_running:
                return LocalWorkerOutcome(STATE_STARTED_SERVICE, "online (service)")
            return LocalWorkerOutcome(STATE_ALREADY_RUNNING, "online (started outside the app)")
        return self._last

    # -- registry and services --------------------------------------------

    def _check_registry_duplicate(self) -> LocalWorkerOutcome | None:
        """An outcome when an online worker already covers this machine, else None.

        A failure to read the registry fails closed: starting a duplicate just
        because the check was unavailable is worse than not starting.
        """
        try:
            workers = parse_workers(self._client.workers())
        except (ControllerError, MonitorUnavailable) as error:
            return self._settle(
                STATE_ERROR, f"cannot check the worker registry: {describe_monitor_error(error)}"
            )
        worker_id = self.config.local_worker.worker_id
        roots = self._roots_mapping()
        stopped_at = self._read_stopped_at()
        for worker in workers:
            if not worker.online or not (worker.worker_id == worker_id or worker.roots == roots):
                continue
            if stopped_at is not None and _stopped_before(worker.last_seen, stopped_at):
                continue  # a dead registration: heartbeat predates our confirmed stop
            return self._settle(
                STATE_ALREADY_RUNNING, f"worker {worker.worker_id!r} is already online"
            )
        return None

    def _ensure_service(self) -> LocalWorkerOutcome:
        """Install/refresh the keep-running service (idempotent per #51)."""
        if not self._service_installed():
            duplicate = self._check_registry_duplicate()
            if duplicate is not None:
                return duplicate
        result = self._run_command(
            service_command(self.worker_config_path(), INSTALL_SERVICE), self._token_env()
        )
        payload = parse_service_payload(result)
        if payload is None:
            return self._settle(
                STATE_ERROR, f"cannot install the local worker service: {service_error(result)}"
            )
        return self._settle(STATE_STARTED_SERVICE, f"service {payload.get('result', 'installed')}")

    def _ensure_child(self) -> LocalWorkerOutcome:
        """Stop a leftover service, then spawn the managed child once."""
        if self._service_installed():
            result = self._run_command(
                service_command(self.worker_config_path(), UNINSTALL_SERVICE), {}
            )
            if parse_service_payload(result) is None:
                return self._settle(
                    STATE_ERROR,
                    f"cannot stop the local worker service: {service_error(result)}",
                )
            self._write_marker()  # the service was just stopped
        duplicate = self._check_registry_duplicate()
        if duplicate is not None:
            return duplicate
        return self._start_child()

    def _service_installed(self) -> bool:
        """Whether the #51 service unit for this worker id is installed."""
        try:
            result = self._run_command(
                service_command(self.worker_config_path(), SERVICE_STATUS), {}
            )
        except OSError:
            return False
        payload = parse_service_payload(result)
        return bool(payload.get("installed")) if payload is not None else False

    # -- child process -----------------------------------------------------

    def _start_child(self) -> LocalWorkerOutcome:
        config_path = self.worker_config_path()
        # Inherit the app environment (PATH, HOME, ...) with the token overlaid;
        # a token-only env left the child without PATH (issue #62).
        env = {**os.environ, **self._token_env()}
        try:
            process = self._spawn(worker_command(config_path), env, config_path.parent)
        except OSError as error:
            return self._settle(STATE_ERROR, f"cannot start the local worker: {error}")
        self._process = process
        return self._settle(STATE_STARTED_CHILD, "started the local worker child process")

    def _terminate_child(self) -> bool:
        """Stop the managed child; True when no child of ours is left running."""
        process = self._process
        if process is None:
            return True
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=CHILD_WAIT_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=KILL_WAIT_SECONDS)
                except subprocess.TimeoutExpired:
                    return False
        self._process = None
        self._write_marker()
        return True

    # -- state on disk -----------------------------------------------------

    def _write_worker_config(self) -> bool:
        content = build_local_worker_toml(
            self.config, worker_config_path=self.worker_config_path()
        )
        path = self.worker_config_path()
        if path.is_file() and path.read_bytes() == content:
            path.chmod(0o600)
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        path.chmod(0o600)
        return True

    def _marker_path(self) -> Path:
        return self.config_path.parent / STATE_FILENAME

    def _read_stopped_at(self) -> datetime | None:
        try:
            payload = json.loads(self._marker_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, Mapping):
            return None
        value = payload.get("stopped_at")
        if not isinstance(value, str):
            return None
        try:
            return _as_utc(datetime.fromisoformat(value))
        except ValueError:
            return None

    def _write_marker(self) -> None:
        payload = {"stopped_at": datetime.now(UTC).isoformat()}
        try:
            self._marker_path().write_text(json.dumps(payload), encoding="utf-8")
        except OSError:
            _log.warning("could not record the local worker stop time")

    # -- helpers -----------------------------------------------------------

    def _roots_mapping(self) -> dict[str, str]:
        return {root.id: str(root.path.expanduser().resolve()) for root in self.config.roots}

    def _token_env(self) -> dict[str, str]:
        return {TOKEN_ENV_VAR: self.config.controller.token or ""}

    def _settle(self, state: str, detail: str = "") -> LocalWorkerOutcome:
        outcome = LocalWorkerOutcome(state, detail)
        self._last = outcome
        return outcome


def _as_utc(when: datetime) -> datetime:
    return when.replace(tzinfo=UTC) if when.tzinfo is None else when


def _stopped_before(last_seen: datetime | None, stopped_at: datetime) -> bool:
    """Whether ``last_seen`` predates a confirmed stop (a dead registration)."""
    if last_seen is None:
        return False
    return _as_utc(last_seen) <= stopped_at
