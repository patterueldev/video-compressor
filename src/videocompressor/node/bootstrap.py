"""Install, update, inspect, and remove one worker on a remote host (issue #53).

The bootstrap probes first and only then changes anything, so the operator
sees the current state before the first mutation. Every step is idempotent: a
rerun reuses the virtualenv, upgrades the wheel, rewrites the config only when
it changed, and re-runs the service install. Uninstall stops the service and
removes the config; ``purge`` also removes the managed directory. Root
mappings, scratch content, and the archive are never deleted.

Nothing here runs with sudo: everything lives under one managed directory
(default ``~/.videocompressor``) and the service itself is installed by the
#51 CLI on the remote host. The #54 Nodes panel uses this class as its backend.
"""

from __future__ import annotations

import re
import shlex
import shutil
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path, PurePosixPath

from videocompressor.node.config import (
    push_worker_config,
    render_worker_toml,
    worker_config_path,
)
from videocompressor.node.distribution import prepare_wheelhouse
from videocompressor.node.models import (
    CommandResult,
    InstallError,
    InstallReport,
    NodeProbe,
    RemoteHost,
    RemoteValidationError,
    StatusReport,
    StepReport,
    UninstallReport,
    WorkerSpec,
)
from videocompressor.node.probe import probe_node
from videocompressor.node.service import (
    confirmed_service_status,
    install_service,
    parse_service_status,
    service_status,
    uninstall_service,
)
from videocompressor.node.ssh import SshRunner
from videocompressor.worker.config import WORKER_ID_PATTERN

__all__ = ["DEFAULT_INSTALL_DIR", "MINIMUM_PYTHON", "NodeBootstrap"]

DEFAULT_INSTALL_DIR = "~/.videocompressor"

#: The project's ``requires-python`` floor; the remote interpreter must match.
MINIMUM_PYTHON = (3, 12)

#: Timeout for slow remote steps (virtualenv creation, pip install).
STEP_TIMEOUT = 600.0

_PYTHON_VERSION = re.compile(r"Python (\d+)\.(\d+)")


class NodeBootstrap:
    """Orchestrates one managed worker install per remote host."""

    def __init__(
        self,
        runner: SshRunner,
        host: RemoteHost,
        *,
        install_dir: str = DEFAULT_INSTALL_DIR,
        python_bin: str = "python3",
        source_dir: Path = Path("."),
        wheelhouse: Path | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.runner = runner
        self.host = host
        self.install_dir = install_dir
        self.python_bin = python_bin
        self.source_dir = source_dir
        self.wheelhouse = wheelhouse
        self._sleep = sleep if sleep is not None else time.sleep

    # -- read-only ---------------------------------------------------------

    def probe(self, *, scratch_dir: str | None = None) -> NodeProbe:
        """Probe the host; when a managed worker exists, also query its service."""
        probe = probe_node(self.runner, install_dir=self.install_dir, scratch_dir=scratch_dir)
        if probe.venv_present and probe.config_present:
            state = parse_service_status(
                service_status(
                    self.runner,
                    venv_dir=self._venv_dir(probe.install_dir),
                    config_path=worker_config_path(probe.install_dir),
                )
            )
            probe.service_ok = state.ok
            probe.service_status = state.summary()
        return probe

    def status(self) -> StatusReport:
        """A read-only snapshot; state problems are reported, not raised."""
        probe = self.probe()
        if probe.venv_present and probe.config_present:
            detail = probe.service_status or "no service output"
            steps = [StepReport("service", ok=probe.service_ok is True, detail=detail)]
        elif not probe.venv_present:
            steps = [StepReport("service", detail="no managed worker install found")]
        else:
            steps = [StepReport("service", detail="worker config missing; service state unknown")]
        return StatusReport(ok=True, probe=probe, steps=steps)

    # -- mutations ---------------------------------------------------------

    def install(self, spec: WorkerSpec) -> InstallReport:
        """Probe, validate, then ship, configure, and start the worker."""
        probe = self.probe(scratch_dir=spec.scratch_dir)
        install_dir = probe.install_dir or _expand_remote_path(self.install_dir, probe.home)
        if not install_dir.startswith("/"):
            raise RemoteValidationError(f"install directory {install_dir!r} must be absolute")
        expanded = replace(
            self._expand_spec(spec, probe),
            worker_id=self._resolve_worker_id(spec.worker_id, probe),
        )
        self._validate_remote(probe, expanded)
        steps = [StepReport("probe", detail=_probe_summary(probe))]

        bundle = prepare_wheelhouse(wheelhouse=self.wheelhouse, source_dir=self.source_dir)
        steps.append(StepReport("wheelhouse", detail=str(bundle.path)))
        try:
            venv_dir = self._venv_dir(install_dir)
            staging = str(PurePosixPath(install_dir) / "staging")
            self._require_ok(
                self.runner.run(
                    f"mkdir -p {shlex.quote(staging)} && chmod 700 {shlex.quote(staging)}"
                ),
                "create the staging directory",
            )
            self.runner.upload_dir(bundle.path, staging)
            steps.append(StepReport("staging", detail=f"uploaded to {staging}"))
            if probe.venv_present:
                steps.append(StepReport("venv", detail=f"reused {venv_dir}"))
            else:
                self._require_ok(
                    self.runner.run(
                        f"{shlex.quote(self.python_bin)} -m venv {shlex.quote(venv_dir)}",
                        timeout=STEP_TIMEOUT,
                    ),
                    "create the virtualenv",
                )
                steps.append(StepReport("venv", detail=f"created {venv_dir}"))

            venv_python = str(PurePosixPath(venv_dir) / "bin" / "python")
            pip_command = (
                f"{shlex.quote(venv_python)} -m pip install --no-index"
                f" --find-links {shlex.quote(staging)} --upgrade video-compressor"
            )
            self._require_ok(
                self.runner.run(pip_command, timeout=STEP_TIMEOUT), "install the worker wheel"
            )
            steps.append(StepReport("install", detail="wheel installed from the staging dir"))

            config_path = worker_config_path(install_dir)
            detail = push_worker_config(
                self.runner, config_path=config_path, text=render_worker_toml(expanded)
            )
            steps.append(StepReport("config", detail=f"{config_path}: {detail}"))

            result = install_service(self.runner, venv_dir=venv_dir, config_path=config_path)
            steps.append(
                StepReport(
                    "service-install",
                    detail=_first_line(result.stdout or result.stderr) or "service installed",
                )
            )
            state = confirmed_service_status(
                self.runner,
                venv_dir=venv_dir,
                config_path=config_path,
                sleep=self._sleep,
            )
            probe.service_ok = state.ok
            probe.service_status = state.summary()
            steps.append(StepReport("service-status", ok=state.ok, detail=state.summary()))
            warnings: list[str] = []
            if not state.ok:
                warnings.append(
                    f"the service is not active after install ({state.summary()}); "
                    "check the service on the remote host"
                )
            version = self._worker_version(venv_dir)
            steps.append(StepReport("version", detail=version or "unknown"))
            return InstallReport(
                ok=True,
                probe=probe,
                worker_id=expanded.worker_id or "",
                steps=steps,
                warnings=warnings,
                worker_version=version,
            )
        finally:
            if bundle.temporary:
                shutil.rmtree(bundle.path, ignore_errors=True)

    def uninstall(self, *, purge: bool = False) -> UninstallReport:
        """Stop the service and remove the config; ``purge`` removes the tree."""
        probe = self.probe()
        install_dir = probe.install_dir or _expand_remote_path(self.install_dir, probe.home)
        config_path = worker_config_path(install_dir)
        venv_dir = self._venv_dir(install_dir)
        steps = [StepReport("probe", detail=_probe_summary(probe))]
        if not probe.venv_present and not probe.config_present:
            steps.append(
                StepReport(
                    "uninstall",
                    detail=f"no managed worker at {install_dir}; nothing to remove",
                )
            )
            return UninstallReport(ok=True, probe=probe, steps=steps)
        if not probe.venv_present:
            raise RemoteValidationError(
                f"the managed virtualenv is missing at {venv_dir}; the service CLI cannot "
                "run (remove the service manually if it still exists)"
            )
        if not probe.config_present:
            raise RemoteValidationError(
                f"the worker config is missing at {config_path}; the service cannot be addressed"
            )
        result = uninstall_service(self.runner, venv_dir=venv_dir, config_path=config_path)
        steps.append(
            StepReport(
                "service-uninstall",
                detail=_first_line(result.stdout or result.stderr) or "service removed",
            )
        )
        self._require_ok(
            self.runner.run(f"rm -f {shlex.quote(config_path)}"), "remove the worker config"
        )
        backup = f"{config_path}.bak"
        backup_present = self.runner.run(f"test -f {shlex.quote(backup)}").returncode == 0
        detail = f"removed {config_path}"
        if backup_present:
            detail += " (kept worker.toml.bak)"
        steps.append(StepReport("config", detail=detail))
        if purge:
            _assert_purgeable(install_dir, probe.home)
            self._require_ok(
                self.runner.run(f"rm -rf {shlex.quote(install_dir)}"),
                "remove the managed install directory",
            )
            steps.append(StepReport("purge", detail=f"removed {install_dir}"))
        return UninstallReport(ok=True, probe=probe, steps=steps)

    # -- helpers -----------------------------------------------------------

    def _expand_spec(self, spec: WorkerSpec, probe: NodeProbe) -> WorkerSpec:
        home = probe.home or "/"
        return replace(
            spec,
            scratch_dir=_expand_remote_path(spec.scratch_dir, home),
            roots={
                root_id: _expand_remote_path(path, home)
                for root_id, path in spec.roots.items()
            },
        )

    def _resolve_worker_id(self, requested: str | None, probe: NodeProbe) -> str:
        if requested:
            if not WORKER_ID_PATTERN.fullmatch(requested):
                raise RemoteValidationError(
                    f"worker id {requested!r} must match [A-Za-z0-9_.-]{{1,64}}"
                )
            return requested
        sanitized = re.sub(r"[^A-Za-z0-9_.-]", "-", probe.hostname or "")[:64]
        if not sanitized or not WORKER_ID_PATTERN.fullmatch(sanitized):
            raise RemoteValidationError(
                "could not derive a worker id from the remote hostname; pass --worker-id"
            )
        return sanitized

    def _validate_remote(self, probe: NodeProbe, spec: WorkerSpec) -> None:
        host = self.host.destination()
        if self.python_bin == "python3":
            version = _python_version(probe.python_version)
            if version is None:
                raise RemoteValidationError(f"python3 was not found on {host}")
            if version < MINIMUM_PYTHON:
                raise RemoteValidationError(
                    f"python3 {probe.python_version} on {host} is too old; "
                    f"Python {MINIMUM_PYTHON[0]}.{MINIMUM_PYTHON[1]}+ is required"
                )
            if not probe.venv_ok:
                raise RemoteValidationError(
                    f"python3 -m venv (with ensurepip) is unavailable on {host}; "
                    "install the venv/ensurepip support package on the remote host"
                )
        else:
            self._require_command(
                f"command -v {shlex.quote(self.python_bin)}",
                f"{self.python_bin} was not found on {host}",
            )
            self._require_command(
                f"{shlex.quote(self.python_bin)} -c 'import venv, ensurepip'",
                f"{self.python_bin} cannot create virtualenvs on {host} "
                "(venv/ensurepip missing)",
            )
        for tool, found in (("ffmpeg", probe.ffmpeg), ("ffprobe", probe.ffprobe)):
            if not found:
                raise RemoteValidationError(
                    f"{tool} was not found on {host}; install ffmpeg on the remote host first"
                )
        for root_id, path in spec.roots.items():
            if not path.startswith("/"):
                raise RemoteValidationError(f"root {root_id!r} path {path!r} must be absolute")
            self._require_command(
                f"test -d {shlex.quote(path)}",
                f"root {root_id!r} path {path} is not a directory on {host}",
            )
        if not spec.scratch_dir.startswith("/"):
            raise RemoteValidationError(f"scratch path {spec.scratch_dir!r} must be absolute")
        self._require_command(
            f"mkdir -p {shlex.quote(spec.scratch_dir)}",
            f"scratch directory {spec.scratch_dir} cannot be created on {host}",
        )
        self._require_command(
            f"test -d {shlex.quote(spec.scratch_dir)}",
            f"scratch directory {spec.scratch_dir} is not a directory on {host}",
        )

    def _require_command(self, command: str, message: str) -> None:
        if self.runner.run(command).returncode != 0:
            raise RemoteValidationError(message)

    def _require_ok(self, result: CommandResult, label: str) -> None:
        if result.returncode != 0:
            detail = _first_line(result.stderr or result.stdout) or f"exit {result.returncode}"
            raise InstallError(f"{label} failed on {self.host.destination()}: {detail}")

    def _worker_version(self, venv_dir: str) -> str | None:
        vc = str(PurePosixPath(venv_dir) / "bin" / "vc")
        result = self.runner.run(f"{shlex.quote(vc)} --version")
        if result.returncode != 0:
            return None
        return _first_line(result.stdout) or None

    def _venv_dir(self, install_dir: str) -> str:
        return str(PurePosixPath(install_dir) / "venv")


def _expand_remote_path(value: str, home: str) -> str:
    """Expand a leading ``~`` against the probed remote home directory."""
    if value == "~":
        return home.rstrip("/") or "/"
    if value.startswith("~/"):
        return f"{home.rstrip('/')}/{value[2:]}"
    return value


def _python_version(text: str | None) -> tuple[int, int] | None:
    if not text:
        return None
    match = _PYTHON_VERSION.search(text)
    return (int(match.group(1)), int(match.group(2))) if match else None


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:160]
    return ""


def _probe_summary(probe: NodeProbe) -> str:
    python = probe.python_version or "python missing"
    ffmpeg = "ffmpeg ok" if probe.ffmpeg else "ffmpeg missing"
    venv = "venv ok" if probe.venv_ok else "venv unavailable"
    return f"{probe.platform or 'unknown platform'}; {python}; {venv}; {ffmpeg}"


def _assert_purgeable(install_dir: str, home: str) -> None:
    normalized = install_dir.rstrip("/") or "/"
    if normalized == "/" or normalized == (home.rstrip("/") or "/"):
        raise RemoteValidationError(
            f"refusing to purge {install_dir}: it is the home directory or the filesystem root"
        )
    if len(PurePosixPath(normalized).parts) < 2:
        raise RemoteValidationError(f"refusing to purge {install_dir}: not a managed subtree")
