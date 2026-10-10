"""Pipeline error mapping: failed vs review vs requeue (issue #6)."""

from __future__ import annotations

import threading

from fixtures.worker import CLIP, FakeEncoder, make_job, raiser
from videocompressor.core.ffmpeg import EncodeError, ScratchSpaceError
from videocompressor.core.fingerprint import fingerprint_file
from videocompressor.core.markers import read_marker
from videocompressor.core.probe import FFprobeUnavailableError, ProbeError
from videocompressor.core.recover import RecoveryOutcome
from videocompressor.core.replace import temp_path_for
from videocompressor.core.validate import ValidationResult
from videocompressor.worker.client import ClaimedJob
from videocompressor.worker.pipeline import JobPipeline, PipelineSeams


def make_pipeline(config, **seams) -> JobPipeline:
    return JobPipeline(config, seams=PipelineSeams(**seams))


def seed_source(config, payload: bytes = b"original-source"):
    source = config.roots["archive"] / CLIP
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(payload)
    return source


def run(pipeline, job=None):
    return pipeline.run(job if job is not None else make_job(), stop_event=threading.Event())


def test_unknown_root_mapping_is_failed(
    worker_config_factory, fake_prober, fake_validator, fake_recover
):
    config = worker_config_factory()
    pipeline = make_pipeline(
        config,
        encode=FakeEncoder(),
        probe=fake_prober(),
        validate=fake_validator(),
        recover=fake_recover(),
    )

    outcome = run(pipeline, make_job(root_id="other"))

    assert outcome.disposition == "failed" and "mapping" in outcome.detail


def test_missing_source_is_review(worker_config_factory):
    # the real recovery seam reports a missing source with no artifacts
    config = worker_config_factory()
    pipeline = make_pipeline(config, encode=FakeEncoder())

    outcome = run(pipeline)

    assert outcome.disposition == "review" and "recovery" in outcome.detail


def test_recovery_needs_review_is_review(
    worker_config_factory, fake_prober, fake_validator, fake_recover
):
    config = worker_config_factory()
    seed_source(config)
    pipeline = make_pipeline(
        config,
        encode=FakeEncoder(),
        probe=fake_prober(),
        validate=fake_validator(),
        recover=fake_recover(RecoveryOutcome.NEEDS_REVIEW, "several backups present"),
    )

    outcome = run(pipeline)

    assert outcome.disposition == "review" and "several backups" in outcome.detail


def test_recovery_confirmed_replacement_is_complete(worker_config_factory, fake_recover):
    config = worker_config_factory()
    source = seed_source(config, payload=b"replacement")
    pipeline = make_pipeline(
        config,
        encode=FakeEncoder(),
        recover=fake_recover(RecoveryOutcome.REPLACEMENT_CONFIRMED),
    )

    outcome = run(pipeline)

    assert outcome.disposition == "complete"
    assert outcome.output == fingerprint_file(source)
    marker = read_marker(source.parent)
    assert marker is not None and "GS010001.MP4" in marker.clips
    assert source.read_bytes() == b"replacement"


def test_unreadable_source_probe_is_review(
    worker_config_factory, fake_validator, fake_recover
):
    config = worker_config_factory()
    seed_source(config)
    pipeline = make_pipeline(
        config,
        encode=FakeEncoder(),
        probe=raiser(ProbeError("source is unreadable")),
        validate=fake_validator(),
        recover=fake_recover(),
    )

    outcome = run(pipeline)

    assert outcome.disposition == "review" and "unreadable" in outcome.detail


def test_missing_ffprobe_is_failed(worker_config_factory, fake_validator, fake_recover):
    config = worker_config_factory()
    seed_source(config)
    pipeline = make_pipeline(
        config,
        encode=FakeEncoder(),
        probe=raiser(FFprobeUnavailableError("ffprobe not found")),
        validate=fake_validator(),
        recover=fake_recover(),
    )

    outcome = run(pipeline)

    assert outcome.disposition == "failed" and "ffprobe unavailable" in outcome.detail


def test_source_without_duration_is_review(
    worker_config_factory, fake_media_factory, fake_prober, fake_validator, fake_recover
):
    config = worker_config_factory()
    seed_source(config)
    pipeline = make_pipeline(
        config,
        encode=FakeEncoder(),
        probe=fake_prober(fake_media_factory(duration_s=None)),
        validate=fake_validator(),
        recover=fake_recover(),
    )

    outcome = run(pipeline)

    assert outcome.disposition == "review" and "no duration" in outcome.detail


def test_scratch_space_shortage_is_failed(
    worker_config_factory, fake_prober, fake_validator, fake_recover
):
    config = worker_config_factory()
    seed_source(config)
    pipeline = make_pipeline(
        config,
        encode=FakeEncoder(),
        probe=fake_prober(),
        validate=fake_validator(),
        recover=fake_recover(),
        scratch_gate=raiser(
            ScratchSpaceError("insufficient scratch space", required_bytes=10, available_bytes=0)
        ),
    )

    outcome = run(pipeline)

    assert outcome.disposition == "failed" and "scratch space" in outcome.detail


def test_encode_error_is_failed(worker_config_factory, fake_prober, fake_validator, fake_recover):
    config = worker_config_factory()
    seed_source(config)
    pipeline = make_pipeline(
        config,
        encode=FakeEncoder(error=EncodeError("x265 pass 2 failed")),
        probe=fake_prober(),
        validate=fake_validator(),
        recover=fake_recover(),
    )

    outcome = run(pipeline)

    assert outcome.disposition == "failed" and "encode failed" in outcome.detail


def test_validation_failure_is_failed(
    worker_config_factory, fake_prober, fake_validator, fake_recover
):
    config = worker_config_factory()
    seed_source(config)
    pipeline = make_pipeline(
        config,
        encode=FakeEncoder(),
        probe=fake_prober(),
        validate=fake_validator(ok=False, failures=("height: expected 720, found 360",)),
        recover=fake_recover(),
    )

    outcome = run(pipeline)

    assert outcome.disposition == "failed" and "height" in outcome.detail


def test_source_changed_before_replacement_is_review(
    worker_config_factory, fake_prober, fake_recover
):
    config = worker_config_factory()
    source = seed_source(config)

    def validate_and_touch(output, settings, *, source_facts=None, **kwargs):
        source.write_bytes(b"changed-elsewhere")
        return ValidationResult(ok=True, failures=(), output=None)

    pipeline = make_pipeline(
        config,
        encode=FakeEncoder(),
        probe=fake_prober(),
        validate=validate_and_touch,
        recover=fake_recover(),
    )

    outcome = run(pipeline)

    assert outcome.disposition == "review" and "source changed" in outcome.detail
    assert source.read_bytes() == b"changed-elsewhere"


def test_swap_artifacts_present_is_review(
    worker_config_factory, fake_prober, fake_validator, fake_recover
):
    config = worker_config_factory()
    source = seed_source(config)
    temp_path_for(source, "old-attempt").write_bytes(b"leftover")

    pipeline = make_pipeline(
        config,
        encode=FakeEncoder(),
        probe=fake_prober(),
        validate=fake_validator(),
        recover=fake_recover(),
    )

    outcome = run(pipeline)

    assert outcome.disposition == "review" and "artifacts" in outcome.detail.lower()


def test_invalid_claim_settings_are_failed(worker_config_factory):
    config = worker_config_factory()
    seed_source(config)
    job = ClaimedJob(root_id="archive", relative_path=CLIP, settings={"output_height": 720})
    pipeline = make_pipeline(config, encode=FakeEncoder())

    outcome = run(pipeline, job)

    assert outcome.disposition == "failed" and "settings" in outcome.detail
