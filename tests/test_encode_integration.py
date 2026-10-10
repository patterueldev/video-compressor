"""Slow integration: synthetic clip through the real two-pass recipe and validation (issue #4)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from videocompressor.core.ffmpeg import run_two_pass_encode
from videocompressor.core.models import EncodeSettings
from videocompressor.core.validate import probe_media_facts, validate_output

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
        reason="ffmpeg/ffprobe are not installed",
    ),
]

CREATION_TIME = "2026-01-02T03:04:05Z"
TIMECODE = "14:16:29:17"
CLIP_SECONDS = 4


@pytest.fixture(scope="module")
def synthetic_source(tmp_path_factory) -> Path:
    """A 4 s 1080p testsrc2 clip with sine audio and an explicit creation_time."""
    path = tmp_path_factory.mktemp("source") / "synthetic.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=1920x1080:rate=30",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=44100",
            "-t",
            str(CLIP_SECONDS),
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-metadata:s:v:0",
            f"timecode={TIMECODE}",
            "-metadata",
            f"creation_time={CREATION_TIME}",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


def test_two_pass_recipe_produces_a_valid_output(synthetic_source: Path, tmp_path: Path):
    settings = EncodeSettings(ffmpeg_preset="veryfast")
    source_facts = probe_media_facts(synthetic_source)
    progress_events = []

    result = run_two_pass_encode(
        synthetic_source,
        tmp_path / "out.mp4",
        settings,
        duration_s=source_facts.duration_s,
        passlog_prefix=tmp_path / "passlog.x265",
        on_progress=progress_events.append,
    )

    assert result.pass_seconds[0] > 0
    assert result.pass_seconds[1] > 0
    assert result.output_bytes == result.output_path.stat().st_size

    output_facts = probe_media_facts(result.output_path)
    assert output_facts.format_name is not None
    assert output_facts.video_codec == "hevc"
    assert output_facts.video_codec_tag == "hvc1"
    assert output_facts.width == 1280
    assert output_facts.height == 720
    assert output_facts.has_audio
    assert output_facts.creation_time == source_facts.creation_time == "2026-01-02T03:04:05.000000Z"
    # The output keeps video + audio + the regenerated timecode track only;
    # no other source data/telemetry streams are carried over.
    assert set(output_facts.stream_codec_tags) == {"hvc1", "mp4a", "tmcd"}

    validation = validate_output(result.output_path, settings, source=source_facts)
    assert validation.ok, validation.failures

    assert progress_events, "expected progress callbacks across both passes"
    assert {event.pass_number for event in progress_events} == {1, 2}
    assert progress_events[-1].percent == pytest.approx(100, abs=2)


def test_output_with_the_wrong_height_fails_validation(synthetic_source: Path, tmp_path: Path):
    small_settings = EncodeSettings(output_height=360, ffmpeg_preset="veryfast")
    source_facts = probe_media_facts(synthetic_source)
    result = run_two_pass_encode(
        synthetic_source,
        tmp_path / "small.mp4",
        small_settings,
        duration_s=source_facts.duration_s,
        passlog_prefix=tmp_path / "passlog.x265",
    )
    validation = validate_output(result.output_path, EncodeSettings(), source=source_facts)
    assert not validation.ok
    assert any(failure.startswith("height:") for failure in validation.failures)
