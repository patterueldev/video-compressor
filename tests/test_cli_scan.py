"""vc scan dry run: classification, report.json, and read-only discipline."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from videocompressor import cli
from videocompressor.cli import EXIT_USAGE, main
from videocompressor.core.fingerprint import fingerprint_file
from videocompressor.core.markers import FolderMarker, MarkerClip, write_marker
from videocompressor.core.models import (
    CaptureTime,
    CaptureTimeMode,
    MediaInfo,
    MediaStream,
)
from videocompressor.core.scan import ClipInspection, derive_root_id

PHT = timezone(timedelta(hours=8))


def _write_file(path: Path, size: int = 8192, seed: int = 0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.write(bytes((i + seed) % 251 for i in range(min(size, 4096))))
        if size > 4096:
            handle.truncate(size)
    return path


def _media(duration_s: float = 120.0, *, audio: bool = True) -> MediaInfo:
    streams = [MediaStream(index=0, codec_type="video", codec_name="hevc", width=1920, height=1080)]
    if audio:
        streams.append(MediaStream(index=1, codec_type="audio", codec_name="aac", channels=2))
    return MediaInfo(
        path=Path("ignored"),
        format_name="mov,mp4",
        duration_s=duration_s,
        size_bytes=8192,
        bit_rate=9_000_000,
        streams=tuple(streams),
    )


def _fake_inspector(table: dict[str, ClipInspection]) -> Callable[[Path], ClipInspection]:
    def inspect(path: Path) -> ClipInspection:
        if path.name not in table:
            raise AssertionError(f"unexpected inspection of {path.name}")
        return table[path.name]

    return inspect


def _build_tree(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "archive"
    folder = root / "2024-06-Ride"
    _write_file(folder / "GS_NEW.MP4")
    done = _write_file(folder / "GS_DONE.MP4")
    _write_file(folder / "GS_REVIEW.MP4")
    write_marker(
        folder,
        FolderMarker(
            template_id="motorcycle",
            settings_hash="deadbeef",
            clips={"GS_DONE.MP4": MarkerClip(output=fingerprint_file(done))},
        ),
    )
    return root, folder


def _capture(when: datetime | None) -> CaptureTime:
    return CaptureTime(
        when=when,
        raw=None,
        source="format",
        mode=CaptureTimeMode.UTC,
    )


def test_scan_dry_run_reports_classifications_and_writes_only_the_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    root, folder = _build_tree(tmp_path)
    monkeypatch.setattr(
        cli,
        "probe_inspector",
        lambda *, capture_resolver=None: _fake_inspector(
            {
                "GS_NEW.MP4": ClipInspection(
                    media=_media(),
                    capture_time=_capture(datetime(2024, 6, 14, 9, 5, tzinfo=PHT)),
                ),
                "GS_REVIEW.MP4": ClipInspection(
                    media=_media(),
                    capture_time=_capture(None),
                    note="capture time unavailable",
                ),
            }
        ),
    )
    monkeypatch.chdir(tmp_path)
    manifest_path = tmp_path / "state" / "manifest.sqlite"

    before = {path for path in tmp_path.rglob("*")}

    exit_code = main(
        [
            "scan",
            "--root",
            str(root),
            "--folder",
            str(folder),
            "--manifest",
            str(manifest_path),
            "--config",
            str(tmp_path / "no-config.toml"),
        ]
    )

    after = {path for path in tmp_path.rglob("*")}
    assert exit_code == 0
    assert after - before == {tmp_path / "report.json"}
    assert not manifest_path.exists()

    stdout = capsys.readouterr().out
    assert "GS_NEW.MP4" in stdout
    assert "complete" in stdout

    report = json.loads((tmp_path / "report.json").read_text())
    assert report["root_id"] == derive_root_id(root)
    assert report["folder"] == "2024-06-Ride"
    assert report["template_id"] == "motorcycle"
    assert report["summary"] == {
        "total": 3,
        "new": 1,
        "complete": 1,
        "changed": 0,
        "review": 1,
        "estimated_bytes": 120 * 896_000 // 8,
    }
    clips = {clip["relative_path"]: clip for clip in report["clips"]}
    assert clips["2024-06-Ride/GS_NEW.MP4"]["classification"] == "new"
    assert clips["2024-06-Ride/GS_NEW.MP4"]["bucket"] == "daytime"
    assert clips["2024-06-Ride/GS_NEW.MP4"]["video_bitrate_kbps"] == 800
    assert clips["2024-06-Ride/GS_DONE.MP4"]["classification"] == "complete"
    assert clips["2024-06-Ride/GS_REVIEW.MP4"]["classification"] == "review"


def test_scan_rejects_a_folder_outside_the_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    root = tmp_path / "archive"
    outside = tmp_path / "elsewhere"
    root.mkdir()
    outside.mkdir()
    monkeypatch.chdir(tmp_path)

    exit_code = main(
        [
            "scan",
            "--root",
            str(root),
            "--folder",
            str(outside),
            "--config",
            str(tmp_path / "c.toml"),
        ]
    )

    assert exit_code == EXIT_USAGE
    assert "outside archive root" in capsys.readouterr().err
    assert not (tmp_path / "report.json").exists()


def test_scan_rejects_an_unknown_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    root, folder = _build_tree(tmp_path)
    monkeypatch.chdir(tmp_path)

    exit_code = main(
        [
            "scan",
            "--root",
            str(root),
            "--folder",
            str(folder),
            "--template",
            "vlog",
            "--config",
            str(tmp_path / "c.toml"),
        ]
    )

    assert exit_code == EXIT_USAGE
    assert "unknown template" in capsys.readouterr().err
    assert not (tmp_path / "report.json").exists()


def test_scan_marks_clips_review_until_issue_2_lands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    root, folder = _build_tree(tmp_path)
    monkeypatch.chdir(tmp_path)

    exit_code = main(
        [
            "scan",
            "--root",
            str(root),
            "--folder",
            str(folder),
            "--manifest",
            str(tmp_path / "state.sqlite"),
            "--config",
            str(tmp_path / "c.toml"),
        ]
    )

    assert exit_code == 0
    stderr = capsys.readouterr().err
    assert "issue #2" in stderr
    report = json.loads((tmp_path / "report.json").read_text())
    clips = {clip["relative_path"]: clip for clip in report["clips"]}
    assert clips["2024-06-Ride/GS_NEW.MP4"]["classification"] == "review"
    assert "issue #2" in clips["2024-06-Ride/GS_NEW.MP4"]["note"]
    # A clip completed before issue #2 still classifies as complete.
    assert clips["2024-06-Ride/GS_DONE.MP4"]["classification"] == "complete"
