"""Shared factories for the Nodes-panel suites (issue #54).

No Qt imports here on purpose: the pure config/service suites use these helpers
and keep running without the optional ``gui`` extra. Hosts and paths are
documentation placeholders only (RFC 5737 / RFC 2606 ranges).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from videocompressor.core.nodes import NodeEntry
from videocompressor.node.models import (
    InstallReport,
    NodeProbe,
    StepReport,
    UninstallReport,
    WorkerSpec,
)

#: Documentation placeholders; never a real host or path.
HOST = "192.0.2.10"
HOME = "/home/example"
INSTALL_DIR = f"{HOME}/.videocompressor"
SCRATCH = f"{HOME}/scratch"
ARCHIVE = f"{HOME}/archive"
CONTROLLER_URL = "http://controller.example.com:8765"
TOKEN = "placeholder-token"


def make_entry(**overrides: object) -> NodeEntry:
    """One plausible config entry, overriding individual fields as needed."""
    fields: dict[str, object] = {
        "id": "node-a",
        "name": "Test node A",
        "host": HOST,
        "user": "example",
        "port": 22,
        "identity": "~/.ssh/id_ed25519",
        "strict_host_keys": False,
        "controller_url": CONTROLLER_URL,
        "roots": {"archive": ARCHIVE},
        "scratch_dir": SCRATCH,
        "thread_cap": 2,
        "install_dir": "",
    }
    fields.update(overrides)
    return NodeEntry(**fields)  # type: ignore[arg-type]


def make_probe(**overrides: object) -> NodeProbe:
    """A worker-less probe report; override keys to describe an install."""
    fields: dict[str, object] = {
        "platform": "Linux",
        "home": HOME,
        "hostname": "node-a",
        "install_dir": INSTALL_DIR,
        "python_bin": "/usr/bin/python3",
        "python_version": "Python 3.12.3",
        "venv_ok": True,
        "ffmpeg": "/usr/bin/ffmpeg",
        "ffprobe": "/usr/bin/ffprobe",
        "ffmpeg_version": "ffmpeg version 6.1.1",
        "disk_free_kb": 52428800,
        "venv_present": False,
        "config_present": False,
    }
    fields.update(overrides)
    return NodeProbe(**fields)  # type: ignore[arg-type]


def make_steps(*steps: tuple[str, bool, str]) -> list[StepReport]:
    """Step reports from ``(step, ok, detail)`` tuples."""
    return [StepReport(step, ok=ok, detail=detail) for step, ok, detail in steps]


def make_install_report(node_id: str = "node-a", **overrides: object) -> InstallReport:
    """A successful install report for ``node_id``."""
    fields: dict[str, object] = {
        "ok": True,
        "probe": make_probe(
            venv_present=True,
            config_present=True,
            worker_version="video-compressor 0.1.0",
            service_status="worker service: running",
            service_ok=True,
        ),
        "worker_id": node_id,
        "steps": make_steps(
            ("probe", True, "Linux; Python 3.12.3; venv ok; ffmpeg ok"),
            ("config", True, "/home/example/.videocompressor/worker.toml: created"),
            ("service-install", True, "worker service installed"),
        ),
        "warnings": [],
        "worker_version": "video-compressor 0.1.0",
    }
    fields.update(overrides)
    return InstallReport(**fields)  # type: ignore[arg-type]


def make_uninstall_report(**overrides: object) -> UninstallReport:
    """A successful uninstall report."""
    fields: dict[str, object] = {
        "ok": True,
        "probe": make_probe(venv_present=True, config_present=True),
        "steps": make_steps(("service-uninstall", True, "worker service removed")),
        "warnings": [],
    }
    fields.update(overrides)
    return UninstallReport(**fields)  # type: ignore[arg-type]


class FakeBootstrap:
    """Scripted ``NodeBootstrap``; records specs and purge flags, or raises one error."""

    def __init__(
        self,
        *,
        probe: NodeProbe | None = None,
        report: InstallReport | None = None,
        uninstall_report: UninstallReport | None = None,
        error: Exception | None = None,
    ) -> None:
        self.probe_result = probe if probe is not None else make_probe()
        self.install_result = report if report is not None else make_install_report()
        self.uninstall_result = (
            uninstall_report if uninstall_report is not None else make_uninstall_report()
        )
        self.error = error
        self.specs: list[WorkerSpec] = []
        self.purges: list[bool] = []

    def probe(self) -> NodeProbe:
        if self.error is not None:
            raise self.error
        return self.probe_result

    def install(self, spec: WorkerSpec) -> InstallReport:
        if self.error is not None:
            raise self.error
        self.specs.append(spec)
        return self.install_result

    def uninstall(self, *, purge: bool = False) -> UninstallReport:
        if self.error is not None:
            raise self.error
        self.purges.append(purge)
        return self.uninstall_result


class FakeBootstrapFactory:
    """Stand-in for the #53 bootstrap factory: records entries, returns one fake."""

    def __init__(self, bootstrap: FakeBootstrap | None = None) -> None:
        self.bootstrap = bootstrap if bootstrap is not None else FakeBootstrap()
        self.entries: list[NodeEntry] = []

    def __call__(self, entry: NodeEntry) -> FakeBootstrap:
        self.entries.append(entry)
        return self.bootstrap


class FakeRegistryClient:
    """Stand-in for ``HttpControllerClient``: one ``GET /workers`` payload or error."""

    def __init__(self, payload: dict[str, Any] | None = None, *, error: Exception | None = None):
        self.payload = payload if payload is not None else {"workers": []}
        self.error = error
        self.calls = 0

    def workers(self) -> dict[str, Any]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.payload


def fake_wheelhouse(base: Path) -> Path:
    """A directory with one project wheel file, for the real-backend wiring test."""
    wheelhouse = base / "wheelhouse"
    wheelhouse.mkdir(parents=True, exist_ok=True)
    (wheelhouse / "video_compressor-0.1.0-py3-none-any.whl").write_bytes(b"wheel")
    return wheelhouse
