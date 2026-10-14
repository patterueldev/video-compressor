"""``vc fix-dates``: summary output, dry-run plumbing, and error paths (issue #86)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from videocompressor.cli import EXIT_ERROR, EXIT_USAGE, fixdates, main
from videocompressor.core.date_stamp import StampReport, StampResult, StampStatus
from videocompressor.core.models import CaptureTimeMode


def _report(tmp_path: Path, results: tuple[StampResult, ...], *, ignored: int = 0) -> StampReport:
    return StampReport(
        folder=tmp_path,
        mode=CaptureTimeMode.LOCAL_WALL_CLOCK,
        timezone="Asia/Manila",
        since=None,
        dry_run=False,
        results=results,
        ignored=ignored,
    )


def test_fix_dates_prints_every_count_and_up_to_ten_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    folder = tmp_path / "compressed"
    folder.mkdir()
    results: list[StampResult] = [
        StampResult(folder / "A.MP4", StampStatus.STAMPED, when=datetime(2024, 6, 14, 9, 5)),
        StampResult(folder / "B.MP4", StampStatus.ALREADY_CORRECT),
        StampResult(folder / "C.MP4", StampStatus.SKIPPED_NO_CAPTURE, raw="junk"),
        StampResult(folder / "D.MP4", StampStatus.SKIPPED_OLDER),
    ]
    results.extend(
        StampResult(folder / f"F{index:02d}.MP4", StampStatus.FAILED, error="not set")
        for index in range(12)
    )
    calls: list[dict] = []

    def fake_sweep(path, **kwargs):
        calls.append({"folder": path, **kwargs})
        return _report(folder, tuple(results), ignored=3)

    monkeypatch.setattr(fixdates, "sweep_folder", fake_sweep)

    exit_code = main(["fix-dates", str(folder), "--config", str(tmp_path / "none.toml")])

    assert exit_code == 0
    assert calls == [
        {
            "folder": folder,
            "mode": CaptureTimeMode.LOCAL_WALL_CLOCK,
            "timezone": "Asia/Manila",
            "dry_run": False,
        }
    ]
    stdout = capsys.readouterr().out
    assert "scanned: 16" in stdout
    assert "stamped: 1" in stdout
    assert "would stamp: 0" in stdout
    assert "already correct: 1" in stdout
    assert "skipped, no capture time: 1" in stdout
    assert "skipped, older than since: 1" in stdout
    assert "ignored: 3" in stdout
    assert "failed: 12" in stdout
    assert stdout.count("failed: " + str(folder)) == 10
    assert "failed: … and 2 more" in stdout


def test_dry_run_is_plumbed_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    folder = tmp_path / "compressed"
    folder.mkdir()
    seen: list[bool] = []

    def fake_sweep(path, *, dry_run, **_kwargs):
        seen.append(dry_run)
        return _report(folder, (StampResult(folder / "A.MP4", StampStatus.WOULD_STAMP),))

    monkeypatch.setattr(fixdates, "sweep_folder", fake_sweep)

    exit_code = main(
        [
            "fix-dates",
            str(folder),
            "--dry-run",
            "--config",
            str(tmp_path / "none.toml"),
        ]
    )

    assert exit_code == 0
    assert seen == [True]
    stdout = capsys.readouterr().out
    assert "would stamp: 1" in stdout
    assert "stamped: 0" in stdout


def test_a_folder_that_is_not_a_directory_is_a_usage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    calls: list[object] = []
    monkeypatch.setattr(fixdates, "sweep_folder", lambda *a, **k: calls.append(1))

    exit_code = main(["fix-dates", str(tmp_path / "missing"), "--config", str(tmp_path / "c.toml")])

    assert exit_code == EXIT_USAGE
    assert "is not a directory" in capsys.readouterr().err
    assert calls == []


def test_non_macos_reports_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    folder = tmp_path / "compressed"
    folder.mkdir()
    monkeypatch.setattr(fixdates, "supported", lambda: False)
    calls: list[object] = []
    monkeypatch.setattr(fixdates, "sweep_folder", lambda *a, **k: calls.append(1))

    exit_code = main(["fix-dates", str(folder), "--config", str(tmp_path / "c.toml")])

    assert exit_code == EXIT_ERROR
    assert "creation dates are only settable on macOS" in capsys.readouterr().err
    assert calls == []
