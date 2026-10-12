"""``vc worker`` service CLI surface (issue #51).

The CLI is exercised through ``main`` with the paths/runner seams patched, so no
test touches a real service manager.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from videocompressor.cli import main
from videocompressor.cli import worker as worker_cli
from videocompressor.worker.service import ServicePaths
from worker.service_support import TOKEN, RecordingRunner, make_paths, write_worker_config


def patch_cli(monkeypatch, tmp_path: Path, platform: str = "linux"):
    paths = make_paths(tmp_path, platform)
    runner = RecordingRunner()
    monkeypatch.setattr(worker_cli, "default_paths", lambda: paths)
    monkeypatch.setattr(worker_cli, "default_runner", runner)
    return paths, runner


def test_help_lists_the_service_surface(capsys) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["worker", "--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    for token in (
        "install-service",
        "service-status",
        "uninstall-service",
        "--config",
        "--once",
        "--dry-run",
        "--json",
    ):
        assert token in out


def test_install_service_json(tmp_path: Path, monkeypatch, capsys) -> None:
    paths, runner = patch_cli(monkeypatch, tmp_path)
    config = write_worker_config(tmp_path)

    assert main(["worker", "install-service", "--config", str(config), "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == 1
    assert payload["operation"] == "install-service"
    assert payload["result"] == "installed"
    assert payload["platform"] == "linux"
    assert payload["scope"] == "systemd_user"
    assert Path(payload["unit"]).is_file()
    assert [tuple(action) for action in payload["actions"]] == runner.calls


def test_flag_order_is_flexible(tmp_path: Path, monkeypatch, capsys) -> None:
    patch_cli(monkeypatch, tmp_path)
    config = write_worker_config(tmp_path)

    assert main(["worker", "--config", str(config), "install-service", "--json"]) == 0

    assert json.loads(capsys.readouterr().out)["result"] == "installed"


def test_missing_config_argument_exits_usage(capsys) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["worker", "install-service"])
    assert excinfo.value.code == 2
    assert "usage" in capsys.readouterr().err.lower()


def test_once_with_a_service_operation_exits_usage(tmp_path: Path, capsys) -> None:
    config = write_worker_config(tmp_path)

    assert main(["worker", "install-service", "--config", str(config), "--once"]) == 2

    assert "--once" in capsys.readouterr().err


def test_foreground_rejects_service_flags(tmp_path: Path, capsys) -> None:
    config = write_worker_config(tmp_path)

    assert main(["worker", "--config", str(config), "--dry-run"]) == 2

    assert "service operations" in capsys.readouterr().err


def test_status_rejects_dry_run(tmp_path: Path, monkeypatch, capsys) -> None:
    patch_cli(monkeypatch, tmp_path)
    config = write_worker_config(tmp_path)

    assert main(["worker", "service-status", "--config", str(config), "--dry-run"]) == 2

    assert "--dry-run" in capsys.readouterr().err


def test_status_not_installed_exits_zero_and_skips_the_probe(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _, runner = patch_cli(monkeypatch, tmp_path)
    config = write_worker_config(tmp_path)

    assert main(["worker", "service-status", "--config", str(config), "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == "not_installed"
    assert payload["installed"] is False
    assert runner.calls == []


def test_config_error_exits_usage(tmp_path: Path, capsys) -> None:
    assert main(["worker", "service-status", "--config", str(tmp_path / "nope.toml")]) == 2
    assert "does not exist" in capsys.readouterr().err


def test_unsupported_platform_exits_operational(tmp_path: Path, monkeypatch, capsys) -> None:
    config = write_worker_config(tmp_path)
    paths = ServicePaths(
        platform="windows", home=tmp_path, config_home=tmp_path, log_dir=tmp_path, uid=0
    )
    monkeypatch.setattr(worker_cli, "default_paths", lambda: paths)

    assert main(["worker", "install-service", "--config", str(config)]) == 1

    assert "unsupported platform" in capsys.readouterr().err


def test_dry_run_preview_redacts_the_token(tmp_path: Path, monkeypatch, capsys) -> None:
    paths, runner = patch_cli(monkeypatch, tmp_path, platform="macos")
    monkeypatch.setenv("VC_TEST_WORKER_TOKEN", TOKEN)
    config = write_worker_config(tmp_path, token_env="VC_TEST_WORKER_TOKEN")

    assert main(["worker", "install-service", "--config", str(config), "--dry-run"]) == 0

    out = capsys.readouterr().out
    assert TOKEN not in out
    assert "redacted" in out
    assert runner.calls == []
    assert not (paths.home / "Library" / "LaunchAgents").exists()


def test_uninstall_removes_the_token_env_file_created_by_install(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    paths, _ = patch_cli(monkeypatch, tmp_path, platform="linux")
    monkeypatch.setenv("VC_TEST_WORKER_TOKEN", TOKEN)
    config = write_worker_config(tmp_path, token_env="VC_TEST_WORKER_TOKEN")

    assert main(["worker", "install-service", "--config", str(config)]) == 0
    env_file = paths.config_home / "videocompressor" / "worker-node-a.env"
    assert env_file.read_text(encoding="utf-8") == f"VC_TEST_WORKER_TOKEN={TOKEN}\n"
    capsys.readouterr()

    assert main(["worker", "uninstall-service", "--config", str(config)]) == 0

    assert not env_file.exists()


def test_status_json_reports_the_env_file_location(tmp_path: Path, monkeypatch, capsys) -> None:
    paths, _ = patch_cli(monkeypatch, tmp_path, platform="linux")
    monkeypatch.setenv("VC_TEST_WORKER_TOKEN", TOKEN)
    config = write_worker_config(tmp_path, token_env="VC_TEST_WORKER_TOKEN")
    assert main(["worker", "install-service", "--config", str(config)]) == 0
    capsys.readouterr()

    assert main(["worker", "service-status", "--config", str(config), "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["installed"] is True
    assert payload["env_file"] == str(paths.config_home / "videocompressor" / "worker-node-a.env")


def test_uninstall_json_and_absent_second_run(tmp_path: Path, monkeypatch, capsys) -> None:
    patch_cli(monkeypatch, tmp_path)
    config = write_worker_config(tmp_path)
    assert main(["worker", "install-service", "--config", str(config)]) == 0
    capsys.readouterr()

    assert main(["worker", "uninstall-service", "--config", str(config), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["operation"] == "uninstall-service"
    assert payload["result"] == "removed"

    assert main(["worker", "uninstall-service", "--config", str(config), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["result"] == "absent"
