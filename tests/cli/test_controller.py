"""CLI controller tests (issue #7: vc controller)."""

from __future__ import annotations

import pytest

from videocompressor.cli import main
from videocompressor.core.config import load_config


def test_controller_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["controller", "--help"])
    assert excinfo.value.code == 0
    assert "--print-token" in capsys.readouterr().out


def test_controller_print_token_generates_and_saves(tmp_path, capsys):
    config_path = tmp_path / "config.toml"

    assert main(["controller", "--config", str(config_path), "--print-token"]) == 0
    token = capsys.readouterr().out.strip()
    assert len(token) >= 32
    assert load_config(config_path).controller.token == token

    # a second call reprints the same stored token
    assert main(["controller", "--config", str(config_path), "--print-token"]) == 0
    assert capsys.readouterr().out.strip() == token


def test_controller_rejects_invalid_port(tmp_path, capsys):
    config_path = tmp_path / "config.toml"
    assert main(["controller", "--config", str(config_path), "--port", "70000"]) == 2
    assert "between 0 and 65535" in capsys.readouterr().err
