"""Slow integration: real ffmpeg through the destination-mode pipeline (issue #40).

Marked ``slow``; needs ffmpeg/ffprobe on PATH. Covers both modes end to end
(destination with originals kept, destination with the verified source delete),
the skip rule on a re-dispatch, and the source-missing resume after a delete.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from fixtures.worker import CLIP, make_job
from videocompressor.core.ffmpeg import run_two_pass_encode
from videocompressor.core.fingerprint import fingerprint_file
from videocompressor.core.probe import probe_media
from videocompressor.core.records import meta_path_for, read_record
from videocompressor.worker.pipeline import JobPipeline, PipelineSeams
from videocompressor.worker.scratch import jobs_root

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
        reason="needs ffmpeg and ffprobe on PATH",
    ),
]

OUTPUT_NAME = Path(CLIP).name


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


def source_path(config) -> Path:
    return config.roots["archive"] / CLIP


def destination_spec(outputs: Path) -> dict:
    return {"kind": "custom_folder", "path": str(outputs)}


def run_job(config, job, *, seams: PipelineSeams | None = None):
    pipeline = JobPipeline(config, seams=seams) if seams is not None else JobPipeline(config)
    return pipeline.run(job, stop_event=threading.Event())


def assert_placed_output(path: Path) -> None:
    facts = probe_media(path)
    video = next(stream for stream in facts.streams if stream.codec_type == "video")
    assert video.codec_name == "hevc"
    assert video.height == 720
    assert any(stream.codec_type == "audio" for stream in facts.streams)
    record = read_record(path)
    assert record is not None and record.output == fingerprint_file(path)
    assert meta_path_for(path).is_file()
    assert not path.with_name(path.name + ".vc-meta.json").exists()


def test_destination_keep_originals_end_to_end(worker_config_factory, tmp_path):
    config = worker_config_factory()
    source = source_path(config)
    make_clip(source)
    original = source.read_bytes()
    outputs = tmp_path / "outputs"
    outputs.mkdir()

    outcome = run_job(
        config,
        make_job(destination=destination_spec(outputs), delete_original=False),
    )

    placed = outputs / OUTPUT_NAME
    assert outcome.disposition == "complete"
    assert outcome.destination == placed
    assert_placed_output(placed)
    assert source.read_bytes() == original
    assert list(jobs_root(config.scratch_dir).iterdir()) == []


def test_destination_delete_original_end_to_end(worker_config_factory, tmp_path):
    config = worker_config_factory()
    source = source_path(config)
    make_clip(source)
    outputs = tmp_path / "outputs"
    outputs.mkdir()

    outcome = run_job(
        config,
        make_job(destination=destination_spec(outputs), delete_original=True),
    )

    placed = outputs / OUTPUT_NAME
    assert outcome.disposition == "complete"
    assert not source.exists()
    assert_placed_output(placed)
    assert list(jobs_root(config.scratch_dir).iterdir()) == []


def test_redispatch_skips_without_a_second_encode(worker_config_factory, tmp_path):
    config = worker_config_factory()
    source = source_path(config)
    make_clip(source)
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    calls: list[int] = []

    def counting_encode(*args, **kwargs):
        calls.append(1)
        return run_two_pass_encode(*args, **kwargs)

    seams = PipelineSeams(encode=counting_encode)
    job = make_job(destination=destination_spec(outputs), delete_original=False)

    first = run_job(config, job, seams=seams)
    assert first.disposition == "complete" and len(calls) == 1
    placed = outputs / OUTPUT_NAME
    placed_stat = placed.stat().st_mtime_ns

    second = run_job(config, job, seams=seams)

    assert second.disposition == "complete" and second.skipped is True
    assert len(calls) == 1  # the skip path never re-encoded
    assert placed.stat().st_mtime_ns == placed_stat
    assert source.is_file()


def test_resume_after_delete_reports_skipped(worker_config_factory, tmp_path):
    config = worker_config_factory()
    source = source_path(config)
    make_clip(source)
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    calls: list[int] = []

    def counting_encode(*args, **kwargs):
        calls.append(1)
        return run_two_pass_encode(*args, **kwargs)

    seams = PipelineSeams(encode=counting_encode)
    job = make_job(destination=destination_spec(outputs), delete_original=True)

    first = run_job(config, job, seams=seams)
    assert first.disposition == "complete" and not source.exists()
    assert len(calls) == 1

    second = run_job(config, job, seams=seams)

    assert second.disposition == "complete" and second.skipped is True
    assert "source is gone" in second.detail
    assert len(calls) == 1
    assert_placed_output(outputs / OUTPUT_NAME)
