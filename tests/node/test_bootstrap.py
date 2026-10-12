"""Bootstrap flows end to end against a fake SSH runner (issue #53)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fixtures.node import (
    ARCHIVE_DIR,
    CONFIG_PATH,
    CONTROLLER_URL,
    HOME,
    INSTALL_DIR,
    SCRATCH_DIR,
    TOKEN,
    WHEEL,
    FakeSshRunner,
    probe_output,
    service_status_json,
)
from videocompressor.node import NodeBootstrap, RemoteHost, WorkerSpec
from videocompressor.node.models import CommandResult, RemoteValidationError, ServiceError
from videocompressor.node.service import STATUS_SETTLE_SECONDS

HOST = RemoteHost(host="192.0.2.10")


def _wheelhouse(tmp_path: Path) -> Path:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir(exist_ok=True)
    (wheelhouse / WHEEL).write_bytes(b"wheel")
    return wheelhouse


def _bootstrap(runner: FakeSshRunner, tmp_path: Path, **overrides: object) -> NodeBootstrap:
    options: dict[str, object] = {"sleep": lambda _: None}
    options.update(overrides)
    return NodeBootstrap(
        runner, HOST, wheelhouse=_wheelhouse(tmp_path), **options  # type: ignore[arg-type]
    )


def _spec(**overrides: object) -> WorkerSpec:
    fields: dict[str, object] = {
        "controller_url": CONTROLLER_URL,
        "token": TOKEN,
        "roots": {"archive": ARCHIVE_DIR},
        "scratch_dir": SCRATCH_DIR,
        "worker_id": "node-a",
    }
    fields.update(overrides)
    fields.update(overrides)
    return WorkerSpec(**fields)  # type: ignore[arg-type]


def test_probe_reports_service_when_a_worker_exists(tmp_path: Path) -> None:
    runner = FakeSshRunner(
        probe=probe_output(venv_present="yes", config_present="yes"), service_rc=0
    )
    probe = _bootstrap(runner, tmp_path).probe()
    assert probe.venv_present is True
    assert probe.config_present is True
    assert probe.service_ok is True
    assert probe.service_status == "active"
    assert runner.commands_with("service-status")


def test_probe_skips_service_without_a_worker(tmp_path: Path) -> None:
    runner = FakeSshRunner()
    probe = _bootstrap(runner, tmp_path).probe()
    assert probe.service_ok is None
    assert not runner.commands_with("service-status")


def test_install_full_flow(tmp_path: Path) -> None:
    runner = FakeSshRunner()
    report = _bootstrap(runner, tmp_path).install(_spec())
    assert report.ok is True
    assert report.worker_id == "node-a"
    assert [step.step for step in report.steps] == [
        "probe",
        "wheelhouse",
        "staging",
        "venv",
        "install",
        "config",
        "service-install",
        "service-status",
        "version",
    ]
    commands = "\n".join(runner.commands)
    assert f"test -d {ARCHIVE_DIR}" in commands
    assert f"mkdir -p {SCRATCH_DIR}" in commands
    assert f"-m venv {INSTALL_DIR}/venv" in commands
    assert "pip install --no-index --find-links" in commands
    assert f"worker install-service --config {CONFIG_PATH}" in commands
    assert runner.uploads and runner.uploads[0][1] == f"{INSTALL_DIR}/staging"
    assert runner.writes and runner.writes[0][0] == CONFIG_PATH
    config_text = runner.writes[0][1]
    assert 'id = "node-a"' in config_text
    assert "token = " in config_text
    assert report.probe.service_ok is True
    assert TOKEN not in json.dumps(report.to_dict())


def test_install_reuses_an_existing_venv(tmp_path: Path) -> None:
    runner = FakeSshRunner(
        probe=probe_output(venv_present="yes", config_present="yes", worker_version="vc 0.1.0")
    )
    report = _bootstrap(runner, tmp_path).install(_spec())
    assert report.ok is True
    assert not runner.commands_with("-m venv")
    pip_commands = runner.commands_with("pip install")
    assert pip_commands and "--force-reinstall" in pip_commands[-1]
    assert not runner.commands_with("--upgrade")
    venv_step = next(step for step in report.steps if step.step == "venv")
    assert "reused" in venv_step.detail


def test_install_step_reports_the_reinstalled_wheel(tmp_path: Path) -> None:
    runner = FakeSshRunner(
        probe=probe_output(venv_present="yes", config_present="yes", worker_version="vc 0.1.0")
    ).on(
        "pip install",
        lambda command: CommandResult(
            command, 0, "Successfully installed tomli-w-1.2.0 video-compressor-0.1.0\n", ""
        ),
    )
    report = _bootstrap(runner, tmp_path).install(_spec())
    step = next(step for step in report.steps if step.step == "install")
    assert step.ok is True
    assert step.detail == "installed video-compressor-0.1.0"


def test_install_step_says_unchanged_when_pip_installs_nothing(tmp_path: Path) -> None:
    runner = FakeSshRunner(
        probe=probe_output(venv_present="yes", config_present="yes", worker_version="vc 0.1.0")
    ).on(
        "pip install",
        lambda command: CommandResult(
            command, 0, "Requirement already satisfied: video-compressor in venv (0.1.0)\n", ""
        ),
    )
    report = _bootstrap(runner, tmp_path).install(_spec())
    step = next(step for step in report.steps if step.step == "install")
    assert step.detail == "unchanged"


def test_install_backs_up_a_differing_config(tmp_path: Path) -> None:
    runner = FakeSshRunner(files={CONFIG_PATH: "old = true\n"})
    report = _bootstrap(runner, tmp_path).install(_spec())
    assert runner.commands_with("cp -p")
    assert runner.writes and runner.writes[0][0] == CONFIG_PATH
    config_step = next(step for step in report.steps if step.step == "config")
    assert "worker.toml.bak" in config_step.detail


def test_install_warns_when_service_status_is_non_zero(tmp_path: Path) -> None:
    runner = FakeSshRunner(service_rc=3, service_out="worker service: not running")
    report = _bootstrap(runner, tmp_path).install(_spec())
    assert report.ok is True
    assert report.warnings
    assert report.probe.service_ok is False


def test_install_reports_the_true_service_state(tmp_path: Path) -> None:
    runner = FakeSshRunner()
    report = _bootstrap(runner, tmp_path).install(_spec())

    assert report.probe.service_ok is True
    assert report.probe.service_status == "active"
    step = next(step for step in report.steps if step.step == "service-status")
    assert step.ok is True


def test_install_warns_when_the_service_is_not_active(tmp_path: Path) -> None:
    runner = FakeSshRunner(service_out=service_status_json("failed", detail="ActiveState=failed"))
    report = _bootstrap(runner, tmp_path).install(_spec())

    assert report.ok is True
    assert report.probe.service_ok is False
    assert report.probe.service_status == "failed: ActiveState=failed"
    assert any("not active" in warning for warning in report.warnings)
    step = next(step for step in report.steps if step.step == "service-status")
    assert step.ok is False


def test_install_rechecks_a_service_that_dies_after_start(tmp_path: Path) -> None:
    runner = FakeSshRunner()
    answers = iter(
        [
            service_status_json("active"),
            service_status_json("failed", detail="ActiveState=failed"),
        ]
    )
    runner.on("service-status", lambda command: CommandResult(command, 0, next(answers), ""))
    sleeps: list[float] = []

    report = _bootstrap(runner, tmp_path, sleep=sleeps.append).install(_spec())

    assert report.probe.service_ok is False
    assert report.probe.service_status == "failed: ActiveState=failed"
    assert sleeps == [STATUS_SETTLE_SECONDS]
    assert any("not active" in warning for warning in report.warnings)


def test_install_defaults_the_worker_id_to_the_hostname(tmp_path: Path) -> None:
    report = _bootstrap(FakeSshRunner(), tmp_path).install(_spec(worker_id=None))
    assert report.worker_id == "node-a"


@pytest.mark.parametrize(
    ("probe_overrides", "message"),
    [
        ({"python_version": "Python 3.11.9"}, "too old"),
        ({"python_version": None}, "python3 was not found"),
        ({"venv_ok": "no"}, "venv"),
        ({"ffmpeg": None}, "ffmpeg"),
        ({"ffprobe": None}, "ffprobe"),
    ],
)
def test_install_validation_failure_changes_nothing(
    tmp_path: Path, probe_overrides: dict[str, object], message: str
) -> None:
    runner = FakeSshRunner(probe=probe_output(**probe_overrides))
    with pytest.raises(RemoteValidationError, match=message):
        _bootstrap(runner, tmp_path).install(_spec())
    assert not runner.commands_with("pip install")
    assert not runner.commands_with("-m venv")
    assert runner.writes == []
    assert runner.uploads == []


def test_install_missing_root_fails_before_mutation(tmp_path: Path) -> None:
    runner = FakeSshRunner().on(f"test -d {ARCHIVE_DIR}", 1)
    with pytest.raises(RemoteValidationError, match="archive"):
        _bootstrap(runner, tmp_path).install(_spec())
    assert not runner.commands_with("pip install")


def test_install_custom_python_runs_its_own_checks(tmp_path: Path) -> None:
    runner = FakeSshRunner(probe=probe_output(python_version="Python 3.11.9"))
    report = _bootstrap(runner, tmp_path, python_bin="python3.13").install(_spec())
    assert report.ok is True
    assert runner.commands_with("command -v python3.13")
    assert runner.commands_with("python3.13 -c 'import venv, ensurepip'")


def test_install_custom_python_missing_fails(tmp_path: Path) -> None:
    runner = FakeSshRunner().on("command -v python3.13", 1)
    with pytest.raises(RemoteValidationError, match="python3.13 was not found"):
        _bootstrap(runner, tmp_path, python_bin="python3.13").install(_spec())


def test_install_surfaces_a_service_failure(tmp_path: Path) -> None:
    runner = FakeSshRunner().on("install-service", 1)
    with pytest.raises(ServiceError, match="install-service failed"):
        _bootstrap(runner, tmp_path).install(_spec())


def test_install_expands_tilde_paths_with_the_remote_home(tmp_path: Path) -> None:
    runner = FakeSshRunner()
    report = _bootstrap(runner, tmp_path).install(
        _spec(roots={"archive": "~/archive"}, scratch_dir="~/scratch")
    )
    config_text = runner.writes[0][1]
    assert f'scratch_dir = "{SCRATCH_DIR}"' in config_text
    assert f'archive = "{ARCHIVE_DIR}"' in config_text
    assert report.ok is True


def test_uninstall_stops_the_service_and_removes_the_config(tmp_path: Path) -> None:
    runner = FakeSshRunner(probe=probe_output(venv_present="yes", config_present="yes"))
    report = _bootstrap(runner, tmp_path).uninstall()
    assert report.ok is True
    assert runner.commands_with("uninstall-service")
    assert runner.commands_with(f"rm -f {CONFIG_PATH}")
    assert not runner.commands_with("rm -rf")


def test_uninstall_purge_removes_the_managed_tree(tmp_path: Path) -> None:
    runner = FakeSshRunner(probe=probe_output(venv_present="yes", config_present="yes"))
    report = _bootstrap(runner, tmp_path).uninstall(purge=True)
    assert report.ok is True
    assert runner.commands_with(f"rm -rf {INSTALL_DIR}")


def test_uninstall_purge_refuses_the_home_directory(tmp_path: Path) -> None:
    runner = FakeSshRunner(
        probe=probe_output(install_dir=HOME, venv_present="yes", config_present="yes")
    )
    bootstrap = _bootstrap(runner, tmp_path)
    with pytest.raises(RemoteValidationError, match="refusing to purge"):
        bootstrap.uninstall(purge=True)


def test_uninstall_without_a_worker_is_a_noop(tmp_path: Path) -> None:
    runner = FakeSshRunner()
    report = _bootstrap(runner, tmp_path).uninstall()
    assert report.ok is True
    assert not runner.commands_with("uninstall-service")


def test_uninstall_with_a_broken_install_fails_explicitly(tmp_path: Path) -> None:
    runner = FakeSshRunner(probe=probe_output(venv_present="no", config_present="yes"))
    with pytest.raises(RemoteValidationError, match="virtualenv is missing"):
        _bootstrap(runner, tmp_path).uninstall()
