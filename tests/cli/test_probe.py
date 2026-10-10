"""CLI probe tests (issue #2: vc probe)."""

from __future__ import annotations

import dataclasses
import subprocess
from datetime import time
from pathlib import Path

import pytest

from videocompressor.cli import EXIT_ERROR, main
from videocompressor.core.config import DEFAULT_TEMPLATE_NAME, AppConfig
from videocompressor.core.models import CaptureIssue, CaptureTimeMode, TemplateSettings
from videocompressor.core.probe import ProbeError

FIXTURES = Path(__file__).parent.parent / "fixtures" / "ffprobe"

NORMAL_RAW = "2024-06-14T17:41:00.000000Z"


def test_probe_local_wall_clock_default_reports_evening(tmp_path, monkeypatch, capsys):
    clip = make_clip(tmp_path)
    use_ffprobe_fixture(monkeypatch, "normal_ztagged.json")
    use_default_config(monkeypatch)

    assert main(["probe", str(clip)]) == 0

    assert capsys.readouterr().out == expected_report(
        clip,
        capture_time="2024-06-14T17:41:00+08:00",
        raw=NORMAL_RAW,
        source="format",
        bucket="evening",
    )


def test_probe_utc_mode_converts_and_reports_daytime(tmp_path, monkeypatch, capsys):
    clip = make_clip(tmp_path)
    use_ffprobe_fixture(monkeypatch, "normal_ztagged.json")
    use_default_config(monkeypatch, mode=CaptureTimeMode.UTC)

    assert main(["probe", str(clip)]) == 0

    assert capsys.readouterr().out == expected_report(
        clip,
        capture_time="2024-06-15T01:41:00+08:00",
        raw=NORMAL_RAW,
        source="format",
        mode="utc",
        bucket="daytime",
    )


def test_probe_missing_timestamp_reports_review(tmp_path, monkeypatch, capsys):
    clip = make_clip(tmp_path)
    use_ffprobe_fixture(monkeypatch, "no_timestamp.json")
    use_default_config(monkeypatch)

    assert main(["probe", str(clip)]) == 0

    assert capsys.readouterr().out == expected_report(clip, review="missing_timestamp")


def test_probe_conflicting_timestamps_report_review(tmp_path, monkeypatch, capsys):
    clip = make_clip(tmp_path)
    use_ffprobe_fixture(monkeypatch, "format_stream_conflict.json")
    use_default_config(monkeypatch)

    assert main(["probe", str(clip)]) == 0

    assert capsys.readouterr().out == expected_report(
        clip, raw=NORMAL_RAW, source="format", review="conflicting_timestamps"
    )


def test_probe_unreadable_file_reports_review(tmp_path, monkeypatch, capsys):
    use_default_config(monkeypatch)
    ghost = tmp_path / "ghost.MP4"

    assert main(["probe", str(ghost)]) == 0

    assert capsys.readouterr().out == expected_report(ghost, review="unreadable")


def test_probe_unsupported_file_reports_review(tmp_path, monkeypatch, capsys):
    clip = make_clip(tmp_path)
    use_default_config(monkeypatch)

    def fake_probe(path):
        raise ProbeError(
            f"{path}: Invalid data found when processing input", kind=CaptureIssue.UNSUPPORTED
        )

    monkeypatch.setattr("videocompressor.cli.probe.probe_media", fake_probe)

    assert main(["probe", str(clip)]) == 0

    assert capsys.readouterr().out == expected_report(clip, review="unsupported")


def test_probe_without_ffprobe_is_an_environment_error(tmp_path, monkeypatch, capsys):
    clip = make_clip(tmp_path)
    use_default_config(monkeypatch)

    def fake_run(command, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", "ffprobe")

    monkeypatch.setattr("videocompressor.core.probe.subprocess.run", fake_run)

    assert main(["probe", str(clip)]) == EXIT_ERROR

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "ffprobe not found" in captured.err


def test_probe_uses_template_timezone_and_evening_start(tmp_path, monkeypatch, capsys):
    clip = make_clip(tmp_path)
    use_ffprobe_fixture(monkeypatch, "normal_ztagged.json")
    template = dataclasses.replace(TemplateSettings(), timezone="UTC", evening_start=time(18, 0))
    use_default_config(monkeypatch, template=template)

    assert main(["probe", str(clip)]) == 0

    assert capsys.readouterr().out == expected_report(
        clip,
        capture_time="2024-06-14T17:41:00+00:00",
        raw=NORMAL_RAW,
        source="format",
        bucket="daytime",
    )


def test_probe_writes_nothing(tmp_path, monkeypatch, capsys):
    folder = tmp_path / "footage"
    folder.mkdir()
    clip = folder / "clip.MP4"
    clip.write_bytes(b"source bytes")
    use_ffprobe_fixture(monkeypatch, "normal_ztagged.json")
    use_default_config(monkeypatch)

    before = snapshot(folder)

    assert main(["probe", str(clip)]) == 0

    capsys.readouterr()
    assert snapshot(folder) == before


def expected_report(
    path: Path,
    *,
    capture_time: str = "none",
    raw: str = "none",
    source: str = "none",
    mode: str = "local_wall_clock",
    bucket: str = "none",
    review: str = "none",
) -> str:
    return (
        f"file: {path}\n"
        f"capture_time: {capture_time}\n"
        f"raw: {raw}\n"
        f"source: {source}\n"
        f"mode: {mode}\n"
        f"bucket: {bucket}\n"
        f"review: {review}\n"
    )


def make_clip(tmp_path: Path) -> Path:
    clip = tmp_path / "clip.MP4"
    clip.write_bytes(b"placeholder bytes")
    return clip


def use_default_config(
    monkeypatch: pytest.MonkeyPatch,
    *,
    mode: CaptureTimeMode = CaptureTimeMode.LOCAL_WALL_CLOCK,
    template: TemplateSettings | None = None,
) -> None:
    config = AppConfig()
    config.capture_time.mode = mode
    if template is not None:
        config.templates[DEFAULT_TEMPLATE_NAME] = template
    monkeypatch.setattr("videocompressor.cli.probe.load_config", lambda: config)


def use_ffprobe_fixture(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    payload = (FIXTURES / name).read_text(encoding="utf-8")

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout=payload, stderr="")

    monkeypatch.setattr("videocompressor.core.probe.subprocess.run", fake_run)


def snapshot(root: Path) -> dict[str, tuple[int, int]]:
    return {
        path.relative_to(root).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in sorted(root.rglob("*"))
    }
