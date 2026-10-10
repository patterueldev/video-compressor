"""Integration: the full protocol and recovery against real media with the real
ffprobe readability check. Marked ``slow``; needs ffmpeg/ffprobe on PATH."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from videocompressor.core.recover import RecoveryOutcome, reconcile_source
from videocompressor.core.replace import (
    backup_path_for,
    ffprobe_readable,
    find_artifacts,
    replace_with_verified_copy,
)

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
        reason="needs ffmpeg and ffprobe on PATH",
    ),
]


def _make_clip(path: Path, *, seconds: int, size: str) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size={size}:rate=15:duration={seconds}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={seconds}",
            "-c:v",
            "mpeg4",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def test_ffprobe_readable_rejects_garbage(tmp_path: Path):
    garbage = tmp_path / "garbage.bin"
    garbage.write_bytes(b"this is not a media file" * 100)

    assert ffprobe_readable(garbage) is False
    assert ffprobe_readable(tmp_path / "does-not-exist.bin") is False


def test_full_protocol_with_real_probe(tmp_path: Path, fingerprint_full):
    source = tmp_path / "GS_DRILL_0001.MP4"
    local_output = tmp_path / "GS_DRILL_0001.out.MP4"
    _make_clip(source, seconds=2, size="320x240")
    _make_clip(local_output, seconds=1, size="160x120")

    assert ffprobe_readable(source) is True

    result = replace_with_verified_copy(
        local_output,
        source,
        expected_source=fingerprint_full(source),
        job_id="integration-1",
        fingerprint=fingerprint_full,
    )

    assert source.read_bytes() == local_output.read_bytes()
    assert ffprobe_readable(source) is True
    assert result.output == fingerprint_full(source)
    assert find_artifacts(source).all == ()


def test_recovery_with_real_probe_restores_unreadable_replacement(tmp_path: Path):
    original = tmp_path / "GS_DRILL_0002.MP4"
    _make_clip(original, seconds=2, size="320x240")
    original_bytes = original.read_bytes()
    source = tmp_path / "GS_DRILL_0002.MP4"
    backup = backup_path_for(source, "crashed-job")
    backup.write_bytes(original_bytes)
    # simulate a crashed swap with a corrupted replacement at the source path
    source.write_bytes(b"corrupted replacement that ffprobe cannot open")

    report = reconcile_source(source)

    assert report.outcome is RecoveryOutcome.ORIGINAL_RESTORED
    assert ffprobe_readable(source) is True
    assert source.read_bytes() == original_bytes
    assert find_artifacts(source).all == ()
