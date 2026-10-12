"""SSH runner and argv construction (issue #53)."""

from __future__ import annotations

from pathlib import Path

import pytest

from videocompressor.node.models import RemoteHost, SshError, SshTimeoutError
from videocompressor.node.ssh import (
    OpenSSHRunner,
    build_scp_argv,
    build_ssh_argv,
    build_ssh_options,
    make_runner,
)


def _shim(tmp_path: Path, name: str, body: str) -> Path:
    script = tmp_path / name
    script.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    script.chmod(0o755)
    return script


def test_ssh_options_default_accept_new() -> None:
    options = build_ssh_options(RemoteHost(host="192.0.2.10"))
    assert "BatchMode=yes" in options
    assert "ConnectTimeout=10" in options
    assert "StrictHostKeyChecking=accept-new" in options
    assert "-p" not in options and "-i" not in options


def test_ssh_options_strict_with_port_and_identity() -> None:
    host = RemoteHost(
        host="192.0.2.10",
        user="operator",
        port=2222,
        identity="/keys/id",
        strict_host_keys=True,
        connect_timeout=3.7,
    )
    options = build_ssh_options(host)
    assert "StrictHostKeyChecking=yes" in options
    assert "ConnectTimeout=4" in options
    assert options[options.index("-p") + 1] == "2222"
    assert options[options.index("-i") + 1] == "/keys/id"


def test_ssh_argv_destination_and_command() -> None:
    argv = build_ssh_argv(RemoteHost(host="192.0.2.10", user="operator"), "echo hello")
    assert argv[-2:] == ["operator@192.0.2.10", "echo hello"]


def test_scp_argv_uses_uppercase_port_and_recursive() -> None:
    host = RemoteHost(host="192.0.2.10", port=2222)
    argv = build_scp_argv(
        host,
        "/tmp/wheels/.",
        "192.0.2.10:/home/example/.videocompressor/staging",
        recursive=True,
    )
    assert "-P" in argv and "-p" not in argv
    assert "-r" in argv
    assert argv[-2:] == ["/tmp/wheels/.", "192.0.2.10:/home/example/.videocompressor/staging"]


def test_make_runner_returns_openssh_runner() -> None:
    assert isinstance(make_runner(RemoteHost(host="192.0.2.10")), OpenSSHRunner)


def test_openssh_runner_invokes_the_ssh_binary(tmp_path: Path, monkeypatch) -> None:
    out = tmp_path / "argv.txt"
    monkeypatch.setenv("SHIM_OUT", str(out))
    shim = _shim(tmp_path, "ssh", 'printf "%s\\n" "$@" > "$SHIM_OUT"')
    runner = OpenSSHRunner(
        RemoteHost(host="192.0.2.10", user="operator", port=2222), ssh_bin=str(shim)
    )
    result = runner.run("echo hello")
    assert result.returncode == 0
    lines = out.read_text(encoding="utf-8").splitlines()
    assert "BatchMode=yes" in lines
    assert "operator@192.0.2.10" in lines
    assert lines[-1] == "echo hello"


def test_openssh_runner_reports_transport_failure(tmp_path: Path) -> None:
    shim = _shim(tmp_path, "ssh", 'echo "Permission denied (publickey)." >&2; exit 255')
    runner = OpenSSHRunner(RemoteHost(host="192.0.2.10"), ssh_bin=str(shim))
    with pytest.raises(SshError, match="Permission denied"):
        runner.run("true")


def test_openssh_runner_times_out(tmp_path: Path) -> None:
    shim = _shim(tmp_path, "ssh", "sleep 5")
    runner = OpenSSHRunner(RemoteHost(host="192.0.2.10"), ssh_bin=str(shim))
    with pytest.raises(SshTimeoutError):
        runner.run("true", timeout=0.2)


def test_write_text_streams_content_on_stdin(tmp_path: Path, monkeypatch) -> None:
    args_out = tmp_path / "argv.txt"
    stdin_out = tmp_path / "stdin.txt"
    monkeypatch.setenv("SHIM_ARGS", str(args_out))
    monkeypatch.setenv("SHIM_STDIN", str(stdin_out))
    shim = _shim(tmp_path, "ssh", 'printf "%s\\n" "$@" > "$SHIM_ARGS"; cat > "$SHIM_STDIN"')
    runner = OpenSSHRunner(RemoteHost(host="192.0.2.10"), ssh_bin=str(shim))
    runner.write_text("/home/example/.videocompressor/worker.toml", "[worker]\n")
    assert stdin_out.read_text(encoding="utf-8") == "[worker]\n"
    command = args_out.read_text(encoding="utf-8").splitlines()[-1]
    assert "umask 077" in command
    assert "chmod 600" in command
    assert "mv " in command


def test_write_text_failure_raises(tmp_path: Path) -> None:
    shim = _shim(tmp_path, "ssh", "exit 1")
    runner = OpenSSHRunner(RemoteHost(host="192.0.2.10"), ssh_bin=str(shim))
    with pytest.raises(SshError, match="could not write"):
        runner.write_text("/x/worker.toml", "body")


def test_upload_dir_uses_scp(tmp_path: Path, monkeypatch) -> None:
    out = tmp_path / "scp.txt"
    monkeypatch.setenv("SHIM_OUT", str(out))
    shim = _shim(tmp_path, "scp", 'printf "%s\\n" "$@" > "$SHIM_OUT"; exit 0')
    local = tmp_path / "wheelhouse"
    local.mkdir()
    (local / "video_compressor-0.1.0-py3-none-any.whl").write_bytes(b"wheel")
    runner = OpenSSHRunner(RemoteHost(host="192.0.2.10"), scp_bin=str(shim))
    runner.upload_dir(local, "/home/example/.videocompressor/staging")
    lines = out.read_text(encoding="utf-8").splitlines()
    assert "-r" in lines
    assert lines[-1] == "192.0.2.10:/home/example/.videocompressor/staging"
    assert lines[-2].endswith("/.")
