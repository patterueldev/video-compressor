"""Probe parsing and assembly (issue #53)."""

from __future__ import annotations

import pytest

from fixtures.node import INSTALL_DIR, SCRATCH_DIR, FakeSshRunner, probe_output
from videocompressor.node.models import ProbeError
from videocompressor.node.probe import parse_probe_output, probe_node


def test_parse_ignores_noise_and_values_with_equals() -> None:
    text = "noise\nVC_PROBE_platform=Linux\nVC_PROBE_marker=a=b\nother: line\n"
    assert parse_probe_output(text) == {"platform": "Linux", "marker": "a=b"}


def test_probe_assembles_fields() -> None:
    runner = FakeSshRunner(
        probe=probe_output(
            scratch_free_kb="123456", venv_present="yes", config_present="yes",
            worker_version="vc 0.1.0",
        )
    )
    probe = probe_node(runner, install_dir=INSTALL_DIR, scratch_dir=SCRATCH_DIR)
    assert probe.platform == "Linux"
    assert probe.home == "/home/example"
    assert probe.hostname == "node-a"
    assert probe.python_version == "Python 3.12.3"
    assert probe.venv_ok is True
    assert probe.ffmpeg == "/usr/bin/ffmpeg"
    assert probe.disk_free_kb == 52428800
    assert probe.scratch_free_kb == 123456
    assert probe.venv_present is True
    assert probe.config_present is True
    assert probe.worker_version == "vc 0.1.0"
    command = runner.commands[0]
    assert f"VC_INSTALL_DIR={INSTALL_DIR}" in command
    assert f"VC_SCRATCH_DIR={SCRATCH_DIR}" in command


def test_probe_without_python_or_ffmpeg() -> None:
    runner = FakeSshRunner(
        probe=probe_output(
            python_bin=None, python_version=None, venv_ok="no", ffmpeg=None, ffprobe=None
        )
    )
    probe = probe_node(runner, install_dir="~/.videocompressor")
    assert probe.python_bin is None
    assert probe.venv_ok is False
    assert probe.ffmpeg is None
    # a leading tilde is passed raw; the remote shell expands it against $HOME
    assert "VC_INSTALL_DIR='~/.videocompressor'" in runner.commands[0]


def test_probe_missing_markers_raises() -> None:
    runner = FakeSshRunner(probe="not a probe\n")
    with pytest.raises(ProbeError, match="VC_PROBE"):
        probe_node(runner, install_dir="~/.videocompressor")


def test_probe_remote_failure_raises() -> None:
    runner = FakeSshRunner().on("mark version 1", 2)
    with pytest.raises(ProbeError, match="probe failed"):
        probe_node(runner, install_dir="~/.videocompressor")
