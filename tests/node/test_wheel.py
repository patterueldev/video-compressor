"""The remote wheel install step and its truthful report (issue #67)."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from fixtures.node import VENV_DIR, FakeSshRunner
from videocompressor.node.models import CommandResult, InstallError
from videocompressor.node.wheel import install_staged_wheel

STAGING = "/home/example/.videocompressor/staging"
VENV_PYTHON = f"{VENV_DIR}/bin/python"
HOST = "192.0.2.10"


def _install(runner: FakeSshRunner, **overrides: str) -> str:
    options: dict[str, str] = {"venv_python": VENV_PYTHON, "staging": STAGING}
    options.update(overrides)
    return install_staged_wheel(runner, host_label=HOST, timeout=600.0, **options)


def _pip_output(text: str, returncode: int = 0) -> Callable[[str], CommandResult]:
    def respond(command: str) -> CommandResult:
        return CommandResult(command, returncode, text, "")

    return respond


def test_install_command_forces_a_reinstall() -> None:
    runner = FakeSshRunner()
    assert _install(runner) == "installed"
    command = runner.commands[-1]
    assert command == (
        f"{VENV_PYTHON} -m pip install --no-index"
        f" --find-links {STAGING} --force-reinstall video-compressor"
    )
    assert "--upgrade" not in command


def test_install_command_quotes_paths() -> None:
    runner = FakeSshRunner()
    _install(runner, venv_python="/opt/venv dir/bin/python", staging="/tmp/staging dir")
    command = runner.commands[-1]
    assert "'/opt/venv dir/bin/python' -m pip install" in command
    assert "--find-links '/tmp/staging dir'" in command


def test_reinstall_reports_the_installed_distribution() -> None:
    runner = FakeSshRunner().on(
        "pip install",
        _pip_output(
            "Processing /staging/video_compressor-0.1.0-py3-none-any.whl\n"
            "Successfully installed tomli-w-1.2.0 tzdata-2025.2 video-compressor-0.1.0\n"
        ),
    )
    assert _install(runner) == "installed video-compressor-0.1.0"


def test_wrapped_summary_line_still_reports_the_distribution() -> None:
    runner = FakeSshRunner().on(
        "pip install",
        _pip_output(
            "Successfully installed tomli-w-1.2.0 tzdata-2025.2\nvideo-compressor-0.1.0\n"
        ),
    )
    assert _install(runner) == "installed video-compressor-0.1.0"


def test_no_install_reports_unchanged() -> None:
    runner = FakeSshRunner().on(
        "pip install",
        _pip_output(
            "Requirement already satisfied: video-compressor in "
            "/home/example/.videocompressor/venv/lib/python3.12/site-packages (0.1.0)\n"
        ),
    )
    assert _install(runner) == "unchanged"


def test_unrecognized_success_still_reports_installed() -> None:
    runner = FakeSshRunner().on("pip install", _pip_output(""))
    assert _install(runner) == "installed"


def test_failure_raises_with_the_first_error_line() -> None:
    runner = FakeSshRunner().on(
        "pip install",
        _pip_output("ERROR: No matching distribution found for tomli-w\nmore detail", returncode=1),
    )
    with pytest.raises(InstallError, match="install the worker wheel failed on 192.0.2.10: ERROR"):
        _install(runner)
