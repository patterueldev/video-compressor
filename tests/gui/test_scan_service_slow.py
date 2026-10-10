"""Slow integration: the GUI scan service over a real ffmpeg clip (issue #8)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from videocompressor.core.models import (
    ArchiveRoot,
    Bucket,
    CaptureTimeMode,
    Classification,
    TemplateSettings,
)
from videocompressor.gui.scan_service import ScanRequest, run_scan

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
        reason="ffmpeg/ffprobe are not installed",
    ),
]

CLIP_SECONDS = 3
# GoPro-style local-with-Z face: under local_wall_clock the face value is the local
# wall clock, so 18:00Z reads as 18:00 in Asia/Manila (evening).
CREATION_TIME = "2026-01-02T18:00:00Z"


def test_scan_service_over_real_clip(tmp_path: Path):
    root = tmp_path / "archive"
    folder = root / "2026-01-Ride"
    folder.mkdir(parents=True)
    clip = folder / "GS010001.MP4"
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
            "testsrc2=size=320x240:rate=15",
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
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-metadata",
            f"creation_time={CREATION_TIME}",
            str(clip),
        ],
        check=True,
        capture_output=True,
    )

    request = ScanRequest(
        root=ArchiveRoot(id="camera", name="Camera", path=root),
        folder=folder,
        template_id="motorcycle",
        settings=TemplateSettings(),
        capture_mode=CaptureTimeMode.LOCAL_WALL_CLOCK,
        manifest_path=None,
    )
    report = run_scan(request)

    (row,) = report.rows
    assert row.classification is Classification.NEW
    assert row.capture_time is not None and row.capture_time.when is not None
    assert row.capture_time.when.isoformat() == "2026-01-02T18:00:00+08:00"
    assert row.bucket is Bucket.EVENING
    assert row.video_bitrate_kbps == 800
    assert row.estimated_bytes is not None and row.estimated_bytes > 0
    assert row.size_bytes == clip.stat().st_size
