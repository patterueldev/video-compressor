"""Unit tests for output validation against source facts and settings (issue #4)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from videocompressor.core.models import EncodeSettings
from videocompressor.core.validate import (
    MediaFacts,
    MediaProbeError,
    duration_tolerance,
    expected_output_width,
    sample_decode_check,
    validate_output,
)

SETTINGS = EncodeSettings()


def make_facts(
    path: str = "out.mp4",
    *,
    format_name: str | None = "mov,mp4,m4a,3gp,3g2,mj2",
    duration_s: float | None = 100.0,
    size_bytes: int | None = 10_000_000,
    video_codec: str | None = "hevc",
    video_codec_tag: str | None = "hvc1",
    width: int | None = 1280,
    height: int | None = 720,
    has_audio: bool = True,
    creation_time: str | None = "2026-01-02T03:04:05.000000Z",
) -> MediaFacts:
    return MediaFacts(
        path=Path(path),
        format_name=format_name,
        duration_s=duration_s,
        size_bytes=size_bytes,
        video_codec=video_codec,
        video_codec_tag=video_codec_tag,
        width=width,
        height=height,
        has_audio=has_audio,
        creation_time=creation_time,
    )


SOURCE = make_facts("source.mp4", video_codec="hevc", width=3840, height=2160, size_bytes=None)


def fake_probe(facts: MediaFacts):
    return lambda path: facts


def fake_run(returncode: int = 0, stderr: str = ""):
    def run(command):
        return subprocess.CompletedProcess(
            args=command, returncode=returncode, stdout="", stderr=stderr
        )

    return run


def validate(facts: MediaFacts, *, source: MediaFacts = SOURCE, **kwargs):
    kwargs.setdefault("decode_check", False)
    return validate_output(
        Path("out.mp4"), SETTINGS, source=source, probe=fake_probe(facts), **kwargs
    )


def test_valid_output_passes_all_checks():
    result = validate(make_facts())
    assert result.ok
    assert result.failures == ()
    assert result.output is not None


def test_wrong_codec_fails():
    result = validate(make_facts(video_codec="mpeg4"))
    assert not result.ok
    assert any(failure.startswith("codec:") for failure in result.failures)


def test_wrong_height_fails():
    result = validate(make_facts(height=1080))
    assert not result.ok
    assert any(failure.startswith("height:") for failure in result.failures)


def test_odd_width_fails():
    result = validate(make_facts(width=1279))
    assert not result.ok
    assert any("odd width" in failure for failure in result.failures)


def test_non_proportional_width_fails():
    result = validate(make_facts(width=1200))
    assert not result.ok
    assert any("proportional" in failure for failure in result.failures)


def test_width_within_rounding_tolerance_passes():
    result = validate(make_facts(width=1278))
    assert result.ok


def test_duration_drift_beyond_tolerance_fails():
    result = validate(make_facts(duration_s=102.0))
    assert not result.ok
    assert any(failure.startswith("duration:") for failure in result.failures)


def test_duration_within_tolerance_passes():
    result = validate(make_facts(duration_s=100.5))
    assert result.ok


def test_audio_presence_must_match_source():
    missing = validate(make_facts(has_audio=False))
    assert not missing.ok
    assert any(failure.startswith("audio:") for failure in missing.failures)

    extra = validate(make_facts(has_audio=True), source=make_facts("source.mp4", has_audio=False))
    assert not extra.ok
    assert any(failure.startswith("audio:") for failure in extra.failures)


def test_unreadable_output_fails_immediately():
    def probe(path):
        raise MediaProbeError(f"cannot probe {path}: invalid data")

    result = validate_output(
        Path("out.mp4"), SETTINGS, source=SOURCE, probe=probe, decode_check=False
    )
    assert not result.ok
    assert len(result.failures) == 1
    assert result.failures[0].startswith("unreadable:")
    assert result.output is None


def test_missing_source_duration_is_a_programming_error():
    with pytest.raises(ValueError, match="source duration"):
        validate_output(
            Path("out.mp4"),
            SETTINGS,
            source=make_facts("source.mp4", duration_s=None),
            probe=fake_probe(make_facts()),
            decode_check=False,
        )


def test_decode_check_failure_is_reported():
    result = validate(
        make_facts(),
        decode_check=True,
        run=fake_run(returncode=1, stderr="Invalid NAL unit size"),
    )
    assert not result.ok
    assert any(failure.startswith("decode:") for failure in result.failures)


def test_decode_check_success_passes():
    result = validate(make_facts(), decode_check=True, run=fake_run())
    assert result.ok


def test_sample_decode_check_samples_start_middle_end_for_long_files():
    calls = []

    def run(command):
        calls.append(command)
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

    assert sample_decode_check(Path("out.mp4"), duration_s=600.0, run=run) is None
    assert len(calls) == 3
    starts = [float(command[command.index("-ss") + 1]) for command in calls]
    assert starts == [0.0, pytest.approx(297.5), pytest.approx(535.5)]


def test_sample_decode_check_decodes_short_files_once():
    calls = []

    def run(command):
        calls.append(command)
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

    assert sample_decode_check(Path("out.mp4"), duration_s=4.0, run=run) is None
    assert len(calls) == 1
    assert calls[0][calls[0].index("-ss") + 1] == "0.000"


def test_expected_output_width_rounds_to_even_pixels():
    assert expected_output_width(3840, 2160, 720) == 1280
    assert expected_output_width(5312, 2988, 720) == 1280
    assert expected_output_width(2993, 2160, 720) == 998
    assert expected_output_width(None, 2160, 720) is None
    assert expected_output_width(3840, None, 720) is None


def test_duration_tolerance_scales_with_long_sources():
    assert duration_tolerance(100.0) == 1.0
    assert duration_tolerance(7200.0) == pytest.approx(7.2)
