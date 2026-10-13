"""ffmpeg/ffprobe directory detection for the service PATH (issue #71)."""

from __future__ import annotations

from pathlib import Path

from videocompressor.worker.toolpaths import (
    FALLBACK_TOOL_DIRS,
    STANDARD_PATH_DIRS,
    resolve_tool,
    service_path_value,
)

STANDARD = "/usr/bin:/bin:/usr/sbin:/sbin"


def test_fallback_and_standard_dirs_are_pinned() -> None:
    assert FALLBACK_TOOL_DIRS == ("/opt/homebrew/bin", "/usr/local/bin")
    assert STANDARD_PATH_DIRS == ("/usr/bin", "/bin", "/usr/sbin", "/sbin")


def test_resolve_tool_prefers_the_path_lookup() -> None:
    found = resolve_tool("ffmpeg", which=lambda tool: "/usr/bin/ffmpeg")

    assert found == "/usr/bin/ffmpeg"


def test_resolve_tool_falls_back_to_the_absolute_locations(tmp_path: Path) -> None:
    brew = tmp_path / "brewbin"
    brew.mkdir()
    tool = brew / "ffmpeg"
    tool.write_text("#!/bin/sh\n", encoding="utf-8")
    tool.chmod(0o755)

    found = resolve_tool("ffmpeg", which=lambda tool: None, fallback_dirs=[str(brew)])

    assert found == str(tool)


def test_resolve_tool_ignores_a_non_executable_candidate(tmp_path: Path) -> None:
    brew = tmp_path / "brewbin"
    brew.mkdir()
    (brew / "ffmpeg").write_text("not executable", encoding="utf-8")

    found = resolve_tool("ffmpeg", which=lambda tool: None, fallback_dirs=[str(brew)])

    assert found is None


def test_resolve_tool_returns_none_when_missing() -> None:
    assert resolve_tool("ffmpeg", which=lambda tool: None, fallback_dirs=[]) is None


def test_service_path_value_puts_detected_dirs_first() -> None:
    resolved = {
        "ffmpeg": "/opt/homebrew/bin/ffmpeg",
        "ffprobe": "/opt/homebrew/bin/ffprobe",
    }.get

    assert service_path_value(resolve=resolved) == f"/opt/homebrew/bin:{STANDARD}"


def test_service_path_value_dedupes_a_standard_dir() -> None:
    resolved = {"ffmpeg": "/usr/bin/ffmpeg", "ffprobe": None}.get

    assert service_path_value(resolve=resolved) == STANDARD


def test_service_path_value_standard_only_when_missing() -> None:
    assert service_path_value(resolve=lambda tool: None) == STANDARD
