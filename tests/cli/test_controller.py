"""CLI controller tests (issue #7: vc controller)."""

from __future__ import annotations

import pytest

from videocompressor.cli import controller as controller_cli
from videocompressor.cli import main
from videocompressor.core.config import AppConfig, load_config, save_config


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


def test_controller_passes_the_configured_orphan_grace(tmp_path, monkeypatch):
    config_path = tmp_path / "config.toml"
    config = AppConfig()
    config.controller.token = "test-token"
    config.controller.orphan_grace_seconds = 120
    save_config(config, config_path)

    captured: dict[str, object] = {}

    class _StubServer:
        """Record the constructor arguments instead of binding a port."""

        base_url = "http://127.0.0.1:0"

        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        def start_background(self) -> None:
            """No-op."""

        def wait(self) -> None:
            """Return immediately so ``run`` completes."""

        def stop(self) -> None:
            """No-op."""

    monkeypatch.setattr(controller_cli, "ControllerServer", _StubServer)
    exit_code = main(
        [
            "controller",
            "--config",
            str(config_path),
            "--manifest",
            str(tmp_path / "manifest.sqlite"),
            "--db",
            str(tmp_path / "controller.sqlite"),
        ]
    )

    assert exit_code == 0
    assert captured["lease_ttl_seconds"] == 120
