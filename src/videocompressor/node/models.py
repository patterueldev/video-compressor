"""Shared types for the remote node bootstrap (issue #53).

The bootstrap drives a worker install on another machine over key-based SSH and
manages everything under one directory on that host (default
``~/.videocompressor``). These dataclasses are the seam the ``vc node`` CLI and
the #54 Nodes panel use; nothing here imports the GUI, the controller, or
PySide6.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field

from videocompressor.worker.config import DEFAULT_THREAD_CAP

__all__ = [
    "CommandResult",
    "InstallError",
    "InstallReport",
    "NodeError",
    "NodeProbe",
    "ProbeError",
    "RemoteHost",
    "RemoteValidationError",
    "ServiceError",
    "SshError",
    "SshTimeoutError",
    "StatusReport",
    "StepReport",
    "UninstallReport",
    "WheelhouseError",
    "WorkerSpec",
]


class NodeError(Exception):
    """Base class for remote node bootstrap failures."""


class SshError(NodeError):
    """The SSH transport failed (connection, auth, or ssh itself)."""

    def __init__(self, message: str, *, stderr: str = "") -> None:
        super().__init__(message)
        self.stderr = stderr


class SshTimeoutError(NodeError):
    """A remote command exceeded its timeout."""


class ProbeError(NodeError):
    """The remote probe output could not be interpreted."""


class RemoteValidationError(NodeError):
    """The remote host or the requested setup is not usable."""


class InstallError(NodeError):
    """A remote install step (virtualenv, pip, file push) failed."""


class ServiceError(NodeError):
    """A worker service operation on the remote failed."""

    def __init__(self, message: str, *, output: str = "") -> None:
        super().__init__(message)
        self.output = output


class WheelhouseError(NodeError):
    """The local wheelhouse is missing or cannot be prepared."""


@dataclass(frozen=True)
class RemoteHost:
    """SSH connection parameters (key-based auth only; no passwords)."""

    host: str
    user: str | None = None
    port: int | None = None
    identity: str | None = None
    strict_host_keys: bool = False
    connect_timeout: float = 10.0

    def destination(self) -> str:
        """``user@host`` when a user is set, else ``host``."""
        return f"{self.user}@{self.host}" if self.user else self.host


@dataclass(frozen=True)
class CommandResult:
    """One remote command's exit code and captured output."""

    command: str
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class StepReport:
    """One announced step of a bootstrap operation."""

    step: str
    ok: bool = True
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        return {"step": self.step, "ok": self.ok, "detail": self.detail}


@dataclass
class NodeProbe:
    """What the remote host reports before anything is changed."""

    platform: str = ""
    home: str = ""
    hostname: str | None = None
    install_dir: str = ""
    python_bin: str | None = None
    python_version: str | None = None
    venv_ok: bool = False
    ffmpeg: str | None = None
    ffprobe: str | None = None
    ffmpeg_version: str | None = None
    disk_free_kb: int | None = None
    scratch_free_kb: int | None = None
    venv_present: bool = False
    config_present: bool = False
    worker_version: str | None = None
    service_status: str | None = None
    service_ok: bool | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class WorkerSpec:
    """The requested worker setup for one remote node.

    ``worker_id`` may be omitted; the bootstrap then derives it from the
    remote hostname. Paths may use a leading ``~``, expanded against the
    remote home directory after the probe.
    """

    controller_url: str
    token: str
    roots: Mapping[str, str]
    scratch_dir: str
    worker_id: str | None = None
    name: str | None = None
    thread_cap: int = DEFAULT_THREAD_CAP
    poll_seconds: float = 5.0


@dataclass
class InstallReport:
    """Every step of an install/update run, in order."""

    ok: bool
    probe: NodeProbe
    worker_id: str
    steps: list[StepReport] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    worker_version: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "worker_id": self.worker_id,
            "worker_version": self.worker_version,
            "probe": self.probe.to_dict(),
            "steps": [step.to_dict() for step in self.steps],
            "warnings": list(self.warnings),
        }


@dataclass
class StatusReport:
    """A read-only status snapshot."""

    ok: bool
    probe: NodeProbe
    steps: list[StepReport] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "probe": self.probe.to_dict(),
            "steps": [step.to_dict() for step in self.steps],
        }


@dataclass
class UninstallReport:
    """Every step of an uninstall run, in order."""

    ok: bool
    probe: NodeProbe
    steps: list[StepReport] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "probe": self.probe.to_dict(),
            "steps": [step.to_dict() for step in self.steps],
            "warnings": list(self.warnings),
        }
