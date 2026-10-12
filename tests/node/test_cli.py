"""vc node CLI wiring: parser, exit codes, JSON output, full lifecycle (issue #53)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fixtures.node import (
    ARCHIVE_DIR,
    CONFIG_PATH,
    CONTROLLER_URL,
    SCRATCH_DIR,
    TOKEN,
    FakeSshRunner,
    probe_output,
    service_status_json,
)
from videocompressor.cli import main
from videocompressor.cli.constants import EXIT_USAGE


@pytest.fixture
def wheelhouse(tmp_path: Path) -> Path:
    directory = tmp_path / "wheelhouse"
    directory.mkdir()
    (directory / "video_compressor-0.1.0-py3-none-any.whl").write_bytes(b"wheel")
    return directory


def _install_args(wheelhouse: Path, **extra: str) -> list[str]:
    args = [
        "node",
        "install",
        "--host",
        "192.0.2.10",
        "--controller-url",
        CONTROLLER_URL,
        "--token-env",
        "VC_NODE_TOKEN",
        "--root",
        f"archive={ARCHIVE_DIR}",
        "--scratch-dir",
        SCRATCH_DIR,
        "--wheelhouse",
        str(wheelhouse),
    ]
    for key, value in extra.items():
        args += [f"--{key.replace('_', '-')}", value]
    return args


def test_node_help_lists_actions(capsys) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["node", "--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert {"probe", "install", "status", "uninstall"} <= set(out.split())


def test_node_without_an_action_is_usage(capsys) -> None:
    assert main(["node"]) == EXIT_USAGE
    assert "choose an action" in capsys.readouterr().err


def test_probe_requires_a_host(capsys) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["node", "probe"])
    assert excinfo.value.code == 2
    assert "usage" in capsys.readouterr().err.lower()


def test_install_requires_exactly_one_token_source(wheelhouse: Path, capsys) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(
            _install_args(wheelhouse)
            + ["--token-file", "/tmp/x", "--token-env", "VC_NODE_TOKEN"]
        )
    assert excinfo.value.code == 2


def test_install_rejects_a_malformed_root(
    wheelhouse: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("VC_NODE_TOKEN", TOKEN)
    args = _install_args(wheelhouse)
    args[args.index(f"archive={ARCHIVE_DIR}")] = "archive"
    assert main(args) == EXIT_USAGE
    assert "--root expects ID=PATH" in capsys.readouterr().err


def test_install_reports_a_missing_token_file(
    wheelhouse: Path, tmp_path: Path, capsys
) -> None:
    args = _install_args(wheelhouse)
    args[args.index("--token-env") + 1] = str(tmp_path / "missing")
    args[args.index("--token-env")] = "--token-file"
    assert main(args) == EXIT_USAGE
    assert "cannot read --token-file" in capsys.readouterr().err


def test_probe_json_output(monkeypatch, capsys) -> None:
    runner = FakeSshRunner()
    monkeypatch.setattr("videocompressor.cli.node.make_runner", lambda host: runner)
    assert main(["node", "probe", "--host", "192.0.2.10", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["platform"] == "Linux"
    assert payload["venv_present"] is False


def test_install_and_status_through_the_cli(
    wheelhouse: Path, monkeypatch, capsys
) -> None:
    runner = FakeSshRunner()
    monkeypatch.setattr("videocompressor.cli.node.make_runner", lambda host: runner)
    monkeypatch.setattr("videocompressor.node.bootstrap.time.sleep", lambda _: None)
    monkeypatch.setenv("VC_NODE_TOKEN", TOKEN)

    assert main(_install_args(wheelhouse)) == 0
    out = capsys.readouterr().out
    assert "worker node-a ready on 192.0.2.10" in out
    assert "(service: running)" in out
    assert TOKEN not in out
    assert runner.writes and runner.writes[0][0] == CONFIG_PATH

    assert main(["node", "status", "--host", "192.0.2.10"]) == 0
    assert "status 192.0.2.10" in capsys.readouterr().out


def test_install_reports_a_dead_service_truthfully(
    wheelhouse: Path, monkeypatch, capsys
) -> None:
    runner = FakeSshRunner(service_out=service_status_json("failed", detail="ActiveState=failed"))
    monkeypatch.setattr("videocompressor.cli.node.make_runner", lambda host: runner)
    monkeypatch.setattr("videocompressor.node.bootstrap.time.sleep", lambda _: None)
    monkeypatch.setenv("VC_NODE_TOKEN", TOKEN)

    assert main(_install_args(wheelhouse)) == 0

    captured = capsys.readouterr()
    assert "worker node-a installed on 192.0.2.10" in captured.out
    assert "(service: failed: ActiveState=failed)" in captured.out
    assert "service: running" not in captured.out
    assert "not active" in captured.err


def test_uninstall_through_the_cli(monkeypatch, capsys) -> None:
    runner = FakeSshRunner(probe=probe_output(venv_present="yes", config_present="yes"))
    monkeypatch.setattr("videocompressor.cli.node.make_runner", lambda host: runner)
    assert main(["node", "uninstall", "--host", "192.0.2.10"]) == 0
    assert runner.commands_with("uninstall-service")
    assert "uninstall complete" in capsys.readouterr().out


def test_runtime_failure_exits_one(monkeypatch, capsys) -> None:
    runner = FakeSshRunner(probe=probe_output(python_version="Python 3.11.9"))
    monkeypatch.setattr("videocompressor.cli.node.make_runner", lambda host: runner)
    monkeypatch.setenv("VC_NODE_TOKEN", TOKEN)
    assert main(_install_args(Path("/nonexistent"))) == 1
    assert "too old" in capsys.readouterr().err
