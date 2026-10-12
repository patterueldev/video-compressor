"""Nodes-panel backend seam: config entries to #53 bootstrap calls (issue #54).

Pure Python — no PySide6 — so the spec building, the activity formatting, and the
registry merge stay testable in CI without the optional ``gui`` extra. All SSH
work is delegated to ``videocompressor.node`` (issue #53) behind an injectable
factory; no SSH logic lives here. Expected backend failures (:class:`NodeError`)
become failed :class:`NodeActivity` values instead of exceptions, while the
transport itself is replaced by a fake in every test.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from videocompressor.core.config import AppConfig
from videocompressor.core.nodes import NodeEntry
from videocompressor.gui.monitor_service import parse_workers
from videocompressor.node import (
    DEFAULT_INSTALL_DIR,
    InstallReport,
    NodeBootstrap,
    NodeError,
    NodeProbe,
    RemoteHost,
    StepReport,
    UninstallReport,
    WorkerSpec,
    make_runner,
)

__all__ = [
    "BootstrapFactory",
    "BootstrapLike",
    "NodeActivity",
    "NodesService",
    "default_controller_url",
    "install_activity",
    "probe_activity",
    "remote_host",
    "uninstall_activity",
]

#: Hosts a remote node cannot use to reach this app's controller.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "0.0.0.0", "::"})


@dataclass(frozen=True)
class NodeActivity:
    """One probe/install/uninstall result for the panel's table and log."""

    node_id: str
    operation: str
    ok: bool
    summary: str
    lines: tuple[str, ...] = ()
    error: str | None = None


class BootstrapLike(Protocol):
    """The slice of ``NodeBootstrap`` the panel uses; scripted in tests."""

    def probe(self) -> NodeProbe: ...

    def install(self, spec: WorkerSpec) -> InstallReport: ...

    def uninstall(self, *, purge: bool = False) -> UninstallReport: ...


#: Builds the backend for one entry; the default constructs ``NodeBootstrap``.
BootstrapFactory = Callable[[NodeEntry], BootstrapLike]


class NodesService:
    """Build #53 backend calls from config entries and format their results."""

    def __init__(
        self,
        config: AppConfig,
        *,
        client: Any | None = None,
        bootstrap_factory: BootstrapFactory | None = None,
        source_dir: Path | None = None,
        wheelhouse: Path | None = None,
    ) -> None:
        self.config = config
        self._client = client
        self._source_dir = source_dir if source_dir is not None else Path.cwd()
        self._wheelhouse = wheelhouse
        self._bootstrap_factory: BootstrapFactory = (
            bootstrap_factory if bootstrap_factory is not None else self._make_bootstrap
        )

    # -- operations --------------------------------------------------------

    def probe(self, entry: NodeEntry) -> NodeActivity:
        """Read-only host report; safe to run before any install decision."""
        try:
            probe = self._bootstrap_factory(entry).probe()
        except NodeError as error:
            return _failed(entry.id, "probe", error)
        return probe_activity(entry.id, probe)

    def install(self, entry: NodeEntry) -> NodeActivity:
        """Ship, configure, and start the worker on the node (idempotent)."""
        try:
            spec = self.worker_spec(entry)
        except ValueError as error:
            return _failed(entry.id, "install", error)
        try:
            report = self._bootstrap_factory(entry).install(spec)
        except NodeError as error:
            return _failed(entry.id, "install", error)
        return install_activity(entry.id, report)

    def uninstall(self, entry: NodeEntry, *, purge: bool = False) -> NodeActivity:
        """Stop and remove the worker service; ``purge`` also removes the tree."""
        try:
            report = self._bootstrap_factory(entry).uninstall(purge=purge)
        except NodeError as error:
            return _failed(entry.id, "uninstall", error)
        return uninstall_activity(entry.id, report)

    # -- inputs ------------------------------------------------------------

    def worker_spec(self, entry: NodeEntry) -> WorkerSpec:
        """The #53 install spec; raises ``ValueError`` when a field is missing."""
        missing: list[str] = []
        if not entry.controller_url.strip():
            missing.append("the controller URL this node uses to reach the controller")
        if not entry.scratch_dir.strip():
            missing.append("the scratch dir on that host")
        if not entry.roots:
            missing.append("at least one root mapping")
        if missing:
            raise ValueError("set " + ", ".join(missing))
        token = (self.config.controller.token or "").strip()
        if not token:
            raise ValueError("the controller token is missing; start the controller from the app")
        return WorkerSpec(
            controller_url=entry.controller_url.strip(),
            token=token,
            roots=dict(entry.roots),
            scratch_dir=entry.scratch_dir.strip(),
            worker_id=entry.id,
            name=entry.name or None,
            thread_cap=entry.thread_cap,
        )

    # -- registry ----------------------------------------------------------

    def registry_online(self) -> set[str] | None:
        """Worker ids the controller counts as online, or ``None`` when unreachable."""
        if self._client is None:
            return None
        try:
            payload = self._client.workers()
        except Exception:  # any controller/transport failure degrades the column
            return None
        return {worker.worker_id for worker in parse_workers(payload) if worker.online}

    # -- backend factory ---------------------------------------------------

    def _make_bootstrap(self, entry: NodeEntry) -> BootstrapLike:
        host = remote_host(entry)
        return NodeBootstrap(
            make_runner(host),
            host,
            install_dir=entry.install_dir or DEFAULT_INSTALL_DIR,
            source_dir=self._source_dir,
            wheelhouse=self._wheelhouse,
        )


def remote_host(entry: NodeEntry) -> RemoteHost:
    """The #53 connection parameters for one config entry (key-based only)."""
    return RemoteHost(
        host=entry.host.strip(),
        user=entry.user.strip() or None,
        port=entry.port,
        identity=entry.identity.strip() or None,
        strict_host_keys=entry.strict_host_keys,
    )


def default_controller_url(config: AppConfig) -> str:
    """Prefill for a node's controller URL; empty for a loopback/wildcard bind.

    A remote node cannot reach the loopback address; when the configured bind is
    not a concrete address the operator must type the LAN/Tailscale address.
    """
    host = config.controller.host.strip()
    if not host or host in LOOPBACK_HOSTS:
        return ""
    return f"http://{host}:{config.controller.port}"


def probe_activity(node_id: str, probe: NodeProbe) -> NodeActivity:
    """Format one read-only probe report for the panel."""
    lines = [
        f"platform: {probe.platform or 'unknown'}",
        f"python: {probe.python_version or 'not found'}"
        f" (venv {'ready' if probe.venv_ok else 'unavailable'})",
        f"ffmpeg: {probe.ffmpeg or 'not found'}"
        f" (ffprobe {'ok' if probe.ffprobe else 'missing'})",
        f"worker: {probe.worker_version or 'not installed'}"
        f" (config {'present' if probe.config_present else 'missing'})",
    ]
    free = probe.scratch_free_kb if probe.scratch_free_kb is not None else probe.disk_free_kb
    if free is not None:
        lines.append(f"free space: {free} kB")
    if probe.service_status is not None:
        state = "running" if probe.service_ok else "not running / unknown"
        lines.append(f"service: {state} ({probe.service_status})")
    else:
        lines.append("service: not present")
    return NodeActivity(
        node_id=node_id,
        operation="probe",
        ok=True,
        summary=_probe_summary(probe),
        lines=tuple(lines),
    )


def install_activity(node_id: str, report: InstallReport) -> NodeActivity:
    """Format one install/update report for the panel."""
    lines = _step_lines(report.steps)
    lines += [f"warning: {warning}" for warning in report.warnings]
    service = (
        "service running" if report.probe.service_ok else "service state uncertain (see steps)"
    )
    summary = f"install ok: worker {report.worker_id or node_id}; {service}"
    return NodeActivity(
        node_id=node_id,
        operation="install",
        ok=report.ok,
        summary=summary,
        lines=tuple(lines),
    )


def uninstall_activity(node_id: str, report: UninstallReport) -> NodeActivity:
    """Format one uninstall report for the panel."""
    lines = _step_lines(report.steps)
    lines += [f"warning: {warning}" for warning in report.warnings]
    return NodeActivity(
        node_id=node_id,
        operation="uninstall",
        ok=report.ok,
        summary="uninstall ok",
        lines=tuple(lines),
    )


# -- helpers ---------------------------------------------------------------


def _probe_summary(probe: NodeProbe) -> str:
    parts = [
        probe.platform or "unknown platform",
        probe.python_version or "python missing",
        "ffmpeg ok" if probe.ffmpeg else "ffmpeg missing",
    ]
    if probe.service_status is not None:
        parts.append("service running" if probe.service_ok else "service not running")
    elif probe.worker_version:
        parts.append(f"worker {probe.worker_version}")
    elif probe.venv_present or probe.config_present:
        parts.append("worker partial")
    else:
        parts.append("worker not installed")
    return "probe ok: " + "; ".join(parts)


def _step_lines(steps: Sequence[StepReport]) -> list[str]:
    lines: list[str] = []
    for step in steps:
        status = "ok" if step.ok else "failed"
        detail = f" - {step.detail}" if step.detail else ""
        lines.append(f"{step.step}: {status}{detail}")
    return lines


def _failed(node_id: str, operation: str, error: BaseException) -> NodeActivity:
    message = str(error) or type(error).__name__
    return NodeActivity(
        node_id=node_id,
        operation=operation,
        ok=False,
        summary=f"{operation} failed: {message}",
        error=message,
    )
