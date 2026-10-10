"""vc scan dry run: classification, report.json, and read-only discipline."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from videocompressor.cli import EXIT_ERROR, EXIT_USAGE, main
from videocompressor.core.fingerprint import fingerprint_file
from videocompressor.core.markers import FolderMarker, MarkerClip, write_marker
from videocompressor.core.models import MediaInfo, MediaStream
from videocompressor.core.scan import derive_root_id


def _write_file(path: Path, size: int = 8192, seed: int = 0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.write(bytes((i + seed) % 251 for i in range(min(size, 4096))))
        if size > 4096:
            handle.truncate(size)
    return path


def _media(
    duration_s: float = 120.0, *, audio: bool = True, creation_time: str | None = None
) -> MediaInfo:
    streams = [MediaStream(index=0, codec_type="video", codec_name="hevc", width=1920, height=1080)]
    if audio:
        streams.append(MediaStream(index=1, codec_type="audio", codec_name="aac", channels=2))
    return MediaInfo(
        path=Path("ignored"),
        format_name="mov,mp4",
        duration_s=duration_s,
        size_bytes=8192,
        bit_rate=9_000_000,
        format_tags={"creation_time": creation_time} if creation_time else {},
        streams=tuple(streams),
    )


def _fake_probe(table: dict[str, MediaInfo]) -> Callable[[Path], MediaInfo]:
    def probe(path: Path) -> MediaInfo:
        if path.name not in table:
            raise AssertionError(f"unexpected probe of {path.name}")
        return table[path.name]

    return probe


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


def test_scan_dry_run_reports_classifications_and_writes_only_the_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    root, folder = _build_tree(tmp_path)
    monkeypatch.setattr(
        "videocompressor.core.scan.probe_media",
        _fake_probe(
            {
                "GS_NEW.MP4": _media(120.0, creation_time="2024-06-14T09:05:00.000000Z"),
                "GS_REVIEW.MP4": _media(60.0),
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
    assert clips["2024-06-Ride/GS_REVIEW.MP4"]["note"] == "capture time missing"


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


def test_scan_without_ffprobe_is_an_environment_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    root, folder = _build_tree(tmp_path)
    monkeypatch.chdir(tmp_path)

    def fake_run(command, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", "ffprobe")

    monkeypatch.setattr("videocompressor.core.probe.subprocess.run", fake_run)

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

    assert exit_code == EXIT_ERROR
    assert "ffprobe not found" in capsys.readouterr().err
    assert not (tmp_path / "report.json").exists()
