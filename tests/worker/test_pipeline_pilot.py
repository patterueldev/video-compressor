"""Pilot-output mode: mirror into the pilot root, never touch sources (issue #6)."""

from __future__ import annotations

import threading

import pytest

from fixtures.worker import CLIP, FakeEncoder, make_job
from videocompressor.core.fingerprint import fingerprint_file
from videocompressor.core.replace import find_artifacts
from videocompressor.worker.pipeline import JobPipeline, PipelineSeams
from videocompressor.worker.scratch import jobs_root
from videocompressor.worker.transfer import PilotDeliveryError, deliver_to_pilot


def make_pipeline(config, **seams) -> JobPipeline:
    return JobPipeline(config, seams=PipelineSeams(**seams))


def test_pilot_mode_writes_to_pilot_root_and_leaves_sources_alone(
    tmp_path, worker_config_factory, fake_prober, fake_validator, fake_recover
):
    pilot = tmp_path / "pilot"
    config = worker_config_factory(pilot_root=pilot)
    source = config.roots["archive"] / CLIP
    source.parent.mkdir(parents=True)
    source.write_bytes(b"original-source")
    original_mtime = source.stat().st_mtime_ns
    replace_calls: list[tuple] = []

    def replace_spy(*args, **kwargs):
        replace_calls.append(args)
        raise AssertionError("pilot mode must never replace the source")

    pipeline = make_pipeline(
        config,
        encode=FakeEncoder(payload=b"pilot-output"),
        probe=fake_prober(),
        validate=fake_validator(),
        recover=fake_recover(),
        replace=replace_spy,
    )
    outcome = pipeline.run(make_job(), stop_event=threading.Event())

    pilot_file = pilot / CLIP
    assert pilot_file.read_bytes() == b"pilot-output"
    assert outcome.disposition == "review"
    assert outcome.pilot is True
    assert str(pilot_file) in outcome.detail
    assert "untouched" in outcome.detail
    # the source is byte-identical and never replaced or marked
    assert source.read_bytes() == b"original-source"
    assert source.stat().st_mtime_ns == original_mtime
    assert not (source.parent / "manifest.json").exists()
    assert find_artifacts(source).all == ()
    assert replace_calls == []
    assert list(jobs_root(config.scratch_dir).iterdir()) == []


def test_pilot_mode_respects_stop_before_delivery(
    tmp_path, worker_config_factory, fake_prober, fake_validator, fake_recover
):
    pilot = tmp_path / "pilot"
    config = worker_config_factory(pilot_root=pilot)
    source = config.roots["archive"] / CLIP
    source.parent.mkdir(parents=True)
    source.write_bytes(b"original-source")
    stop = threading.Event()

    class StopAfterEncode(FakeEncoder):
        def __call__(self, *args, **kwargs):
            result = super().__call__(*args, **kwargs)
            stop.set()
            return result

    pipeline = make_pipeline(
        config,
        encode=StopAfterEncode(),
        probe=fake_prober(),
        validate=fake_validator(),
        recover=fake_recover(),
    )
    outcome = pipeline.run(make_job(), stop_event=stop)

    assert outcome.disposition == "requeue" and "before transfer" in outcome.detail
    assert not (pilot / CLIP).exists()
    assert source.read_bytes() == b"original-source"


def test_deliver_to_pilot_verification_failure(tmp_path, monkeypatch):
    output = tmp_path / "out.mp4"
    output.write_bytes(b"good-output")

    def bad_copy(source, destination, stop_event, **kwargs):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"corrupted")

    monkeypatch.setattr("videocompressor.worker.transfer.copy_with_stop", bad_copy)
    with pytest.raises(PilotDeliveryError, match="did not verify"):
        deliver_to_pilot(
            output,
            tmp_path / "pilot",
            "folder/clip.mp4",
            threading.Event(),
            fingerprint=fingerprint_file,
        )
