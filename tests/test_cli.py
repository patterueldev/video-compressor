"""CLI tests (issue #1 acceptance: vc --version; clear not-implemented stubs)."""

import pytest

from videocompressor import __version__
from videocompressor.cli import EXIT_NOT_IMPLEMENTED, main


def test_version_prints_and_exits_zero(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert capsys.readouterr().out.strip() == f"vc {__version__}"


@pytest.mark.parametrize("command", ["worker", "controller", "gui"])
def test_subcommand_stubs_exit_clearly(command, capsys):
    assert main([command]) == EXIT_NOT_IMPLEMENTED
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"vc {command}: not implemented yet" in captured.err


def test_scan_requires_root_and_folder(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["scan"])
    assert excinfo.value.code == 2
    assert "usage" in capsys.readouterr().err.lower()


def test_no_command_prints_usage(capsys):
    assert main([]) == EXIT_NOT_IMPLEMENTED
    assert "usage" in capsys.readouterr().err.lower()
