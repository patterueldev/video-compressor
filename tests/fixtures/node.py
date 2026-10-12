"""Node-domain fixtures (issue #53): a fake SSH runner and probe output.

Every remote interaction in the node suites goes through the fake runner, so
the tests exercise the real command construction without network or SSH. All
hosts and paths are documentation placeholders (192.0.2.0/24, example.com).
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

import pytest

from videocompressor.node.models import CommandResult

HOST = "192.0.2.10"
HOME = "/home/example"
INSTALL_DIR = f"{HOME}/.videocompressor"
VENV_DIR = f"{INSTALL_DIR}/venv"
CONFIG_PATH = f"{INSTALL_DIR}/worker.toml"
SCRATCH_DIR = f"{HOME}/scratch"
ARCHIVE_DIR = f"{HOME}/archive"
CONTROLLER_URL = "http://controller.example.com:8765"
TOKEN = "placeholder-token"
WHEEL = "video_compressor-0.1.0-py3-none-any.whl"


def probe_output(**overrides: object) -> str:
    """A plausible probe report; override keys, ``None`` drops them."""
    values: dict[str, str] = {
        "version": "1",
        "platform": "Linux",
        "home": HOME,
        "hostname": "node-a",
        "install_dir": INSTALL_DIR,
        "python_bin": "/usr/bin/python3",
        "python_version": "Python 3.12.3",
        "venv_ok": "yes",
        "ffmpeg": "/usr/bin/ffmpeg",
        "ffmpeg_version": "ffmpeg version 6.1.1",
        "ffprobe": "/usr/bin/ffprobe",
        "disk_free_kb": "52428800",
        "venv_present": "no",
        "config_present": "no",
    }
    values.update({key: str(value) for key, value in overrides.items() if value is not None})
    for key, value in overrides.items():
        if value is None:
            values.pop(key, None)
    return "".join(f"VC_PROBE_{key}={value}\n" for key, value in values.items())


def service_status_json(
    state: str = "active", *, detail: str = "", installed: bool = True
) -> str:
    """A schema-1 ``vc worker service-status --json`` document."""
    return json.dumps(
        {
            "schema": 1,
            "operation": "service-status",
            "installed": installed,
            "state": state,
            "detail": detail,
        }
    )


class FakeSshRunner:
    """A scripted SshRunner double that records every remote interaction."""

    def __init__(
        self,
        *,
        probe: str | None = None,
        service_rc: int = 0,
        service_out: str = service_status_json(),
        files: dict[str, str] | None = None,
    ) -> None:
        self.commands: list[str] = []
        self.writes: list[tuple[str, str]] = []
        self.uploads: list[tuple[str, str]] = []
        self.probe = probe if probe is not None else probe_output()
        self.service_rc = service_rc
        self.service_out = service_out
        self.files: dict[str, str] = dict(files or {})
        self.handlers: list[tuple[str, Any]] = []

    def on(self, matcher: str, response: Any) -> FakeSshRunner:
        """Respond to commands containing ``matcher``.

        ``response`` is a :class:`CommandResult`, an exit code, an exception to
        raise, or a callable taking the command and returning a result.
        """
        self.handlers.append((matcher, response))
        return self

    def run(self, command: str, *, timeout: float = 30.0) -> CommandResult:
        self.commands.append(command)
        for matcher, response in self.handlers:
            if matcher in command:
                return self._resolve(response, command)
        if "mark version 1" in command:
            return CommandResult(command, 0, self.probe, "")
        if "service-status" in command:
            stderr = "" if self.service_rc == 0 else "service not running"
            return CommandResult(command, self.service_rc, self.service_out, stderr)
        if "install-service" in command:
            return CommandResult(command, 0, "worker service installed", "")
        if "uninstall-service" in command:
            return CommandResult(command, 0, "worker service removed", "")
        if command.startswith("test -f "):
            path = shlex.split(command[len("test -f "):])[0]
            return CommandResult(command, 0 if path in self.files else 1, "", "")
        if command.startswith("cat "):
            path = shlex.split(command[len("cat "):])[0]
            if path in self.files:
                return CommandResult(command, 0, self.files[path], "")
            return CommandResult(command, 1, "", "no such file")
        return CommandResult(command, 0, "", "")

    def _resolve(self, response: Any, command: str) -> CommandResult:
        if isinstance(response, Exception):
            raise response
        if callable(response):
            return response(command)
        if isinstance(response, int):
            return CommandResult(command, response, "", "")
        return response

    def write_text(self, remote_path: str, text: str) -> None:
        self.writes.append((remote_path, text))
        self.files[remote_path] = text

    def upload_dir(self, local_dir: Path, remote_dir: str) -> None:
        self.uploads.append((str(local_dir), remote_dir))

    def commands_with(self, fragment: str) -> list[str]:
        return [command for command in self.commands if fragment in command]


@pytest.fixture
def fake_ssh_runner() -> FakeSshRunner:
    """A fresh fake runner with default (bare host) probe output."""
    return FakeSshRunner()
