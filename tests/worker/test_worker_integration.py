"""Slow integration: real ffmpeg through ``vc worker`` (issue #6).

Marked ``slow``; needs ffmpeg/ffprobe on PATH. Covers the acceptance path
(``vc worker --config`` on a fixture root with a verified replacement), the
SIGTERM mid-encode stop with a resume to completion, and pilot-output mode.
"""

from __future__ import annotations

import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from fixtures.controller import TOKEN
from fixtures.worker import CLIP
from videocompressor.cli import main
from videocompressor.core.fingerprint import fingerprint_file
from videocompressor.core.markers import read_marker
from videocompressor.core.models import JobState
from videocompressor.core.probe import probe_media
from videocompressor.core.replace import find_artifacts
from videocompressor.worker.scratch import jobs_root

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
        reason="needs ffmpeg and ffprobe on PATH",
    ),
]


def make_clip(path: Path, *, seconds: int = 2, size: str = "320x240") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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


def write_worker_config(tmp_path: Path, server, *, pilot: Path | None = None) -> Path:
    archive = tmp_path / "archive"
    archive.mkdir(exist_ok=True)
    scratch = tmp_path / "scratch"
    body = f"""
[worker]
id = "it-node"
controller_url = "{server.base_url}"
token = "{TOKEN}"
scratch_dir = "{scratch}"
heartbeat_seconds = 0.1

[roots]
archive = "{archive}"
"""
    if pilot is not None:
        body += f'\n[pilot]\nroot = "{pilot}"\n'
    path = tmp_path / "worker.toml"
    path.write_text(body, encoding="utf-8")
    return path


def archive_source(tmp_path: Path) -> Path:
    return tmp_path / "archive" / CLIP


def wait_until(predicate, timeout: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def video_stream(path: Path):
    facts = probe_media(path)
    return facts, next(stream for stream in facts.streams if stream.codec_type == "video")


def test_worker_once_replaces_clip_end_to_end(server, seed_clip, manifest, tmp_path: Path):
    source = archive_source(tmp_path)
    make_clip(source, seconds=2)
    config_path = write_worker_config(tmp_path, server)
    seed_clip(CLIP)

    assert main(["worker", "--config", str(config_path), "--once"]) == 0

    facts, video = video_stream(source)
    assert video.codec_name == "hevc"
    assert video.height == 720
    assert any(stream.codec_type == "audio" for stream in facts.streams)
    assert find_artifacts(source).all == ()

    record = manifest.get_clip("archive", CLIP)
    assert record.state is JobState.COMPLETE
    assert record.output == fingerprint_file(source)
    marker = read_marker(source.parent)
    assert marker is not None and "GS010001.MP4" in marker.clips
    assert list(jobs_root(tmp_path / "scratch").iterdir()) == []


def test_sigterm_mid_encode_leaves_source_intact_then_resumes(
    server, seed_clip, manifest, tmp_path: Path
):
    source = archive_source(tmp_path)
    make_clip(source, seconds=5, size="640x480")
    original = source.read_bytes()
    config_path = write_worker_config(tmp_path, server)
    seed_clip(CLIP)

    process = subprocess.Popen(
        [sys.executable, "-m", "videocompressor.cli", "worker", "--config", str(config_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert wait_until(
            lambda: manifest.get_clip("archive", CLIP).state is JobState.ENCODING
        ), "worker never reached the encoding stage"
        process.send_signal(signal.SIGTERM)
        _, stderr = process.communicate(timeout=60)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()

    assert process.returncode == 0, stderr
    assert source.read_bytes() == original
    assert find_artifacts(source).all == ()
    assert manifest.get_clip("archive", CLIP).state is JobState.QUEUED

    # resume: a second run restarts the clip from the beginning and completes it
    assert main(["worker", "--config", str(config_path), "--once"]) == 0
    assert manifest.get_clip("archive", CLIP).state is JobState.COMPLETE
    _, video = video_stream(source)
    assert video.codec_name == "hevc"


def test_worker_pilot_mode_writes_to_pilot_root(server, seed_clip, manifest, tmp_path: Path):
    source = archive_source(tmp_path)
    make_clip(source, seconds=2)
    original = source.read_bytes()
    original_mtime = source.stat().st_mtime_ns
    pilot = tmp_path / "pilot"
    config_path = write_worker_config(tmp_path, server, pilot=pilot)
    seed_clip(CLIP)

    assert main(["worker", "--config", str(config_path), "--once"]) == 0

    pilot_file = pilot / CLIP
    assert pilot_file.is_file()
    _, video = video_stream(pilot_file)
    assert video.codec_name == "hevc" and video.height == 720
    assert source.read_bytes() == original
    assert source.stat().st_mtime_ns == original_mtime
    assert find_artifacts(source).all == ()
    assert not (source.parent / "manifest.json").exists()

    record = manifest.get_clip("archive", CLIP)
    assert record.state is JobState.REVIEW
    assert "pilot output" in record.error
