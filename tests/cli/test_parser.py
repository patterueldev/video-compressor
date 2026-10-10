"""CLI parser and dispatch tests (issue #1: version and stubs; issue #22: cli package)."""

from __future__ import annotations

import pytest

from videocompressor import __version__
from videocompressor.cli import EXIT_NOT_IMPLEMENTED, main


def test_version_prints_and_exits_zero(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert capsys.readouterr().out.strip() == f"vc {__version__}"


def test_scan_requires_root_and_folder(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["scan"])
    assert excinfo.value.code == 2
    assert "usage" in capsys.readouterr().err.lower()


def test_no_command_prints_usage(capsys):
    assert main([]) == EXIT_NOT_IMPLEMENTED
    assert "usage" in capsys.readouterr().err.lower()


def test_probe_requires_a_file_argument(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["probe"])
    assert excinfo.value.code == 2
    assert "usage" in capsys.readouterr().err.lower()
