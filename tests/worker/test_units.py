"""Unit-file rendering for the worker service (issue #51)."""

from __future__ import annotations

import plistlib
from pathlib import Path

from videocompressor.worker.service import ServiceSpec
from videocompressor.worker.units import (
    REDACTED,
    env_file_path,
    launchd_label,
    log_paths,
    render_launchd_plist,
    render_systemd_unit,
    render_unit,
    systemd_unit_name,
    unit_path,
)
from worker.service_support import COMMAND, PATH_VALUE, TOKEN, WORKER_ID, make_paths, make_spec


def test_launchd_names_paths_and_logs(tmp_path: Path) -> None:
    paths = make_paths(tmp_path, "macos")
    spec = make_spec(tmp_path / "worker.toml")

    assert launchd_label(WORKER_ID) == "com.videocompressor.worker.node-a"
    assert unit_path(spec, paths) == (
        paths.home / "Library" / "LaunchAgents" / "com.videocompressor.worker.node-a.plist"
    )
    assert env_file_path(spec, paths) is None
    assert log_paths(spec, paths) == {
        "stdout": str(paths.log_dir / "node-a.out.log"),
        "stderr": str(paths.log_dir / "node-a.err.log"),
    }


def test_systemd_names_paths_and_logs(tmp_path: Path) -> None:
    paths = make_paths(tmp_path, "linux")
    spec = make_spec(tmp_path / "worker.toml")

    assert systemd_unit_name(WORKER_ID) == "videocompressor-worker-node-a.service"
    assert unit_path(spec, paths) == (
        paths.config_home / "systemd" / "user" / "videocompressor-worker-node-a.service"
    )
    # The env-file path is deterministic for the worker id, whether or not the
    # spec came from a full config read (the file is only written for token_env).
    assert env_file_path(spec, paths) == (
        paths.config_home / "videocompressor" / "worker-node-a.env"
    )
    assert log_paths(spec, paths) == {"journal": "videocompressor-worker-node-a.service"}


def test_systemd_token_env_file_path(tmp_path: Path) -> None:
    paths = make_paths(tmp_path, "linux")
    spec = make_spec(tmp_path / "worker.toml", token_env="VC_WORKER_TOKEN", token_value=TOKEN)

    assert env_file_path(spec, paths) == (
        paths.config_home / "videocompressor" / "worker-node-a.env"
    )


def test_launchd_plist_payload(tmp_path: Path) -> None:
    paths = make_paths(tmp_path, "macos")
    spec = make_spec(tmp_path / "worker.toml")

    payload = plistlib.loads(render_launchd_plist(spec, paths))

    assert payload == {
        "Label": "com.videocompressor.worker.node-a",
        "ProgramArguments": list(COMMAND),
        "WorkingDirectory": str(spec.config_path.parent),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 30,
        "ProcessType": "Background",
        "StandardOutPath": str(paths.log_dir / "node-a.out.log"),
        "StandardErrorPath": str(paths.log_dir / "node-a.err.log"),
        "EnvironmentVariables": {"PATH": PATH_VALUE},
    }


def test_launchd_plist_carries_and_redacts_the_token(tmp_path: Path) -> None:
    paths = make_paths(tmp_path, "macos")
    spec = make_spec(tmp_path / "worker.toml", token_env="VC_WORKER_TOKEN", token_value=TOKEN)

    payload = plistlib.loads(render_launchd_plist(spec, paths))
    assert payload["EnvironmentVariables"] == {"VC_WORKER_TOKEN": TOKEN, "PATH": PATH_VALUE}

    preview = render_unit(spec, paths, redact_token=True)
    assert isinstance(preview, bytes)
    assert TOKEN not in preview.decode()
    assert plistlib.loads(preview)["EnvironmentVariables"] == {
        "VC_WORKER_TOKEN": REDACTED,
        "PATH": PATH_VALUE,
    }


def test_units_embed_the_service_path(tmp_path: Path) -> None:
    """Both platforms carry an explicit PATH so a brew-only ffmpeg resolves (#71)."""
    spec = make_spec(tmp_path / "worker.toml")

    payload = plistlib.loads(render_launchd_plist(spec, make_paths(tmp_path, "macos")))
    assert payload["EnvironmentVariables"]["PATH"] == PATH_VALUE

    text = render_systemd_unit(spec, make_paths(tmp_path, "linux"))
    assert f"Environment=PATH={PATH_VALUE}\n" in text

    spaced = make_spec(tmp_path / "worker.toml", path_value="/opt/my tools/bin:/usr/bin")
    spaced_text = render_systemd_unit(spaced, make_paths(tmp_path, "linux"))
    assert 'Environment="PATH=/opt/my tools/bin:/usr/bin"\n' in spaced_text


def test_systemd_unit_text(tmp_path: Path) -> None:
    paths = make_paths(tmp_path, "linux")
    spec = make_spec(tmp_path / "worker.toml")

    expected = (
        "[Unit]\n"
        "Description=Video Compressor worker node-a\n"
        "After=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"WorkingDirectory={spec.config_path.parent}\n"
        "ExecStart=/opt/venv/bin/python -m videocompressor.cli worker "
        "--config /abs/worker.toml\n"
        f"Environment=PATH={PATH_VALUE}\n"
        "Restart=on-failure\n"
        "RestartSec=30\n"
        "StandardOutput=journal\n"
        "StandardError=journal\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )

    assert render_systemd_unit(spec, paths) == expected


def test_systemd_unit_uses_environment_file_not_the_value(tmp_path: Path) -> None:
    paths = make_paths(tmp_path, "linux")
    spec = make_spec(tmp_path / "worker.toml", token_env="VC_WORKER_TOKEN", token_value=TOKEN)

    text = render_systemd_unit(spec, paths)

    assert f"EnvironmentFile={env_file_path(spec, paths)}\n" in text
    assert TOKEN not in text


def test_systemd_inline_token_has_no_environment_file(tmp_path: Path) -> None:
    paths = make_paths(tmp_path, "linux")
    spec = make_spec(tmp_path / "worker.toml")

    assert "EnvironmentFile=" not in render_systemd_unit(spec, paths)


def test_systemd_quoting_and_specifier_escaping(tmp_path: Path) -> None:
    paths = make_paths(tmp_path, "linux")
    spec = ServiceSpec(
        worker_id=WORKER_ID,
        config_path=Path('/tmp/50% done/worker "x".toml'),
        command=("/Applications/My Tools/python", "-m", "videocompressor.cli"),
        path_value=PATH_VALUE,
    )

    text = render_systemd_unit(spec, paths)

    assert 'WorkingDirectory="/tmp/50%% done"' in text
    assert 'ExecStart="/Applications/My Tools/python" -m videocompressor.cli\n' in text


def test_render_unit_returns_platform_document_types(tmp_path: Path) -> None:
    spec = make_spec(tmp_path / "worker.toml")

    assert isinstance(render_unit(spec, make_paths(tmp_path, "macos")), bytes)
    assert isinstance(render_unit(spec, make_paths(tmp_path, "linux")), str)
