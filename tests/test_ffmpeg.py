"""Unit tests for the two-pass x265 recipe, progress parsing, and scratch gate (issue #4)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from videocompressor.core import ffmpeg
from videocompressor.core.ffmpeg import (
    EncodeProgress,
    ProgressParser,
    ScratchSpaceError,
    build_pass1_command,
    build_pass2_command,
    check_scratch_space,
    compute_eta,
    estimate_output_bytes,
    required_scratch_bytes,
)
from videocompressor.core.models import EncodeSettings

FIXTURES = Path(__file__).parent / "fixtures" / "progress"

SETTINGS = EncodeSettings()


def test_pass1_command_recipe():
    command = build_pass1_command(SETTINGS, Path("in.mp4"), passlog_prefix=Path("scratch/job.x265"))
    assert command == [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-y",
        "-progress",
        "pipe:1",
        "-nostats",
        "-i",
        "in.mp4",
        "-map",
        "0:v:0",
        "-vf",
        "scale=-2:720",
        "-pix_fmt",
        "yuv420p",
        "-c:v",
        "libx265",
        "-b:v",
        "800k",
        "-preset",
        "medium",
        "-pass",
        "1",
        "-passlogfile",
        "scratch/job.x265",
        "-an",
        "-f",
        "null",
        "-",
    ]


def test_pass2_command_recipe():
    command = build_pass2_command(
        SETTINGS, Path("in.mp4"), Path("out.mp4"), passlog_prefix=Path("scratch/job.x265")
    )
    assert command == [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-y",
        "-progress",
        "pipe:1",
        "-nostats",
        "-i",
        "in.mp4",
        "-map",
        "0:v:0",
        "-vf",
        "scale=-2:720",
        "-pix_fmt",
        "yuv420p",
        "-c:v",
        "libx265",
        "-b:v",
        "800k",
        "-preset",
        "medium",
        "-pass",
        "2",
        "-passlogfile",
        "scratch/job.x265",
        "-map",
        "0:a?",
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-map_metadata",
        "0",
        "-map_chapters",
        "-1",
        "-tag:v",
        "hvc1",
        "-movflags",
        "+faststart",
        "out.mp4",
    ]


def test_command_respects_custom_settings_and_thread_cap():
    settings = EncodeSettings(
        output_height=480, video_bitrate_kbps=1200, audio_bitrate_kbps=64, ffmpeg_preset="slow"
    )
    command = build_pass1_command(
        settings,
        Path("in.mp4"),
        passlog_prefix=Path("log"),
        threads=2,
        ffmpeg_bin="/opt/ffmpeg",
    )
    assert command[0] == "/opt/ffmpeg"
    assert command[command.index("-vf") + 1] == "scale=-2:480"
    assert command[command.index("-b:v") + 1] == "1200k"
    assert command[command.index("-preset") + 1] == "slow"
    assert command[command.index("-threads") + 1] == "2"


def replay_fixture(name: str, *, pass_number: int, duration_s: float) -> list[EncodeProgress]:
    parser = ProgressParser(pass_number=pass_number, duration_s=duration_s)
    emitted = []
    for line in (FIXTURES / name).read_text(encoding="utf-8").splitlines():
        progress = parser.feed(line)
        if progress is not None:
            emitted.append(progress)
    return emitted


def test_progress_parser_replays_pass1_fixture():
    emitted = replay_fixture("pass1.txt", pass_number=1, duration_s=6.0)
    assert len(emitted) >= 2
    assert all(item.pass_number == 1 for item in emitted)
    assert all(item.percent is not None and 0 <= item.percent <= 100 for item in emitted)
    final = emitted[-1]
    assert final.frame is not None and final.frame > 0
    assert final.speed is not None and final.speed > 0
    assert final.out_time_s == pytest.approx(5.933333, abs=0.05)
    assert final.eta_seconds is not None and final.eta_seconds >= 0
    assert final.total_size_bytes is None  # pass 1 writes to the null muxer


def test_progress_parser_replays_pass2_fixture():
    emitted = replay_fixture("pass2.txt", pass_number=2, duration_s=6.0)
    final = emitted[-1]
    assert final.pass_number == 2
    assert final.total_size_bytes is not None and final.total_size_bytes > 0
    assert final.percent is not None and final.percent > 95
    times = [item.out_time_s for item in emitted]
    assert all(item is not None for item in times)
    assert times == sorted(times)


def test_progress_parser_returns_none_until_block_completes():
    parser = ProgressParser(pass_number=1, duration_s=10.0)
    assert parser.feed("frame=10") is None
    assert parser.feed("") is None
    assert parser.feed("out_time=00:00:01.500000") is None
    progress = parser.feed("progress=continue")
    assert progress is not None
    assert progress.out_time_s == pytest.approx(1.5)
    assert progress.frame == 10


def test_progress_parser_tolerates_na_values():
    parser = ProgressParser(pass_number=2, duration_s=10.0)
    for line in ("frame=N/A", "fps=N/A", "speed=N/A", "total_size=N/A", "out_time=N/A"):
        assert parser.feed(line) is None
    progress = parser.feed("progress=end")
    assert progress is not None
    assert progress.frame is None
    assert progress.fps is None
    assert progress.speed is None
    assert progress.out_time_s is None
    assert progress.percent is None
    assert progress.eta_seconds is None


def test_compute_eta():
    assert compute_eta(600.0, 300.0, 1.5) == pytest.approx(200.0)
    assert compute_eta(600.0, 600.0, 2.0) == 0.0
    assert compute_eta(600.0, 650.0, 2.0) == 0.0
    assert compute_eta(None, 300.0, 1.5) is None
    assert compute_eta(600.0, None, 1.5) is None
    assert compute_eta(600.0, 300.0, None) is None
    assert compute_eta(600.0, 300.0, 0.0) is None


def test_estimate_output_bytes_matches_reference_sizes():
    # plan.md reference: 800 kbps video for 15 minutes is about 90 MB.
    assert estimate_output_bytes(900, 800, 0) == 90_000_000
    assert estimate_output_bytes(900, 800, 96) == 100_800_000
    assert estimate_output_bytes(900, 800, 96, audio_stream_count=2) == 111_600_000
    assert estimate_output_bytes(0, 800, 96) == 0


def test_required_scratch_bytes_sums_source_output_logs_margin():
    required = required_scratch_bytes(
        source_bytes=1_000_000,
        estimated_output_bytes=2_000_000,
        pass_log_allowance_bytes=500,
        margin_bytes=10_000,
    )
    assert required == 3_010_500


def test_check_scratch_space_passes_with_enough_room(monkeypatch, tmp_path):
    monkeypatch.setattr(ffmpeg, "disk_usage", lambda path: SimpleNamespace(free=5_000_000))
    report = check_scratch_space(
        tmp_path,
        source_bytes=1_000_000,
        estimated_output_bytes=1_000_000,
        pass_log_allowance_bytes=500,
        margin_bytes=10_000,
    )
    assert report.ok
    assert report.available_bytes == 5_000_000
    assert report.required_bytes == 2_010_500


def test_check_scratch_space_raises_when_short(monkeypatch, tmp_path):
    monkeypatch.setattr(ffmpeg, "disk_usage", lambda path: SimpleNamespace(free=10))
    with pytest.raises(ScratchSpaceError, match="insufficient scratch space") as excinfo:
        check_scratch_space(
            tmp_path,
            source_bytes=1_000_000,
            estimated_output_bytes=1_000_000,
            pass_log_allowance_bytes=500,
            margin_bytes=10_000,
        )
    assert excinfo.value.required_bytes == 2_010_500
    assert excinfo.value.available_bytes == 10
