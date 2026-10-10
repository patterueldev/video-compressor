"""The ``vc gui`` entry point: lazy PySide6 load and argument pass-through (issue #8)."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from videocompressor.cli import EXIT_ERROR, main
from videocompressor.cli import gui as cli_gui


def _args(*, config: Path | None = None, manifest: Path | None = None) -> argparse.Namespace:
    return argparse.Namespace(config=config, manifest=manifest)


def test_gui_help_prints_usage(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["gui", "--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "vc gui" in out
    assert "--config" in out
    assert "--manifest" in out


def test_missing_pyside6_reports_install_hint(monkeypatch, capsys):
    def missing():
        raise ModuleNotFoundError("No module named 'PySide6'", name="PySide6")

    monkeypatch.setattr(cli_gui, "_load_run_gui", missing)
    assert main(["gui"]) == EXIT_ERROR
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "gui extra" in captured.err
    assert "pip install" in captured.err


def test_unrelated_missing_module_is_not_swallowed(monkeypatch):
    def missing():
        raise ModuleNotFoundError("No module named 'something'", name="something")

    monkeypatch.setattr(cli_gui, "_load_run_gui", missing)
    with pytest.raises(ModuleNotFoundError):
        cli_gui.run(_args())


def test_arguments_reach_the_app(monkeypatch):
    calls: dict[str, object] = {}

    def fake_run(**kwargs):
        calls.update(kwargs)
        return 0

    monkeypatch.setattr(cli_gui, "_load_run_gui", lambda: fake_run)
    config = Path("/tmp/config.toml")
    manifest = Path("/tmp/manifest.sqlite")

    assert cli_gui.run(_args(config=config, manifest=manifest)) == 0
    assert calls == {"config_path": config, "manifest_path": manifest}
