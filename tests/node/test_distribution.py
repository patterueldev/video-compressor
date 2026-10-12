"""Wheelhouse preparation for wheel-over-SSH distribution (issue #53)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from fixtures.node import WHEEL
from videocompressor.node.distribution import (
    Wheelhouse,
    find_project_wheel,
    prepare_wheelhouse,
)
from videocompressor.node.models import WheelhouseError


def test_provided_wheelhouse_is_validated(tmp_path: Path) -> None:
    wheelhouse = tmp_path / "wheels"
    wheelhouse.mkdir()
    (wheelhouse / WHEEL).write_bytes(b"wheel")
    bundle = prepare_wheelhouse(wheelhouse=wheelhouse, source_dir=tmp_path)
    assert bundle == Wheelhouse(wheelhouse.resolve(), temporary=False)


def test_provided_wheelhouse_without_the_project_wheel(tmp_path: Path) -> None:
    wheelhouse = tmp_path / "wheels"
    wheelhouse.mkdir()
    (wheelhouse / "tomli_w-1.2.0-py3-none-any.whl").write_bytes(b"wheel")
    with pytest.raises(WheelhouseError, match="no video_compressor"):
        prepare_wheelhouse(wheelhouse=wheelhouse, source_dir=tmp_path)


def test_missing_wheelhouse_directory(tmp_path: Path) -> None:
    with pytest.raises(WheelhouseError, match="not a directory"):
        prepare_wheelhouse(wheelhouse=tmp_path / "nope", source_dir=tmp_path)


def test_build_requires_a_project_checkout(tmp_path: Path) -> None:
    source = tmp_path / "checkout"
    source.mkdir()
    with pytest.raises(WheelhouseError, match="pyproject.toml"):
        prepare_wheelhouse(wheelhouse=None, source_dir=source)


def test_build_invokes_pip_and_returns_a_temporary_wheelhouse(tmp_path: Path) -> None:
    source = tmp_path / "checkout"
    source.mkdir()
    (source / "pyproject.toml").write_text("[project]\nname = 'video-compressor'\n")
    calls: list[list[str]] = []

    def fake_run(argv: list[str], *, capture_output: bool, text: bool):
        calls.append(list(argv))
        wheel_dir = Path(argv[argv.index("--wheel-dir") + 1])
        (wheel_dir / WHEEL).write_bytes(b"wheel")
        return subprocess.CompletedProcess(argv, 0, "built", "")

    bundle = prepare_wheelhouse(wheelhouse=None, source_dir=source, runner=fake_run)
    try:
        assert calls and calls[0][1:4] == ["-m", "pip", "wheel"]
        assert calls[0][-1] == str(source.resolve())
        assert bundle.temporary is True
        assert find_project_wheel(bundle.path).name == WHEEL
    finally:
        shutil.rmtree(bundle.path, ignore_errors=True)


def test_build_failure_raises(tmp_path: Path) -> None:
    source = tmp_path / "checkout"
    source.mkdir()
    (source / "pyproject.toml").write_text("[project]\n")

    def fake_run(argv: list[str], *, capture_output: bool, text: bool):
        return subprocess.CompletedProcess(argv, 1, "", "no network")

    with pytest.raises(WheelhouseError, match="no network"):
        prepare_wheelhouse(wheelhouse=None, source_dir=source, runner=fake_run)
