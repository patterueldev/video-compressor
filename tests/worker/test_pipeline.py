"""Pipeline core flow: verified replacement, stop/resume, copy verification (issue #6)."""

from __future__ import annotations

import threading

from fixtures.worker import CLIP, FakeEncoder, make_job, raiser
from videocompressor.core.fingerprint import fingerprint_file
from videocompressor.core.markers import read_marker
from videocompressor.core.models import Fingerprint
from videocompressor.core.replace import VerificationError, find_artifacts
from videocompressor.worker.pipeline import JobPipeline, PipelineSeams
from videocompressor.worker.scratch import jobs_root, prepare_scratch_root


def make_pipeline(config, **seams) -> JobPipeline:
    return JobPipeline(config, seams=PipelineSeams(**seams))


def seed_source(config, relative_path: str = CLIP, payload: bytes = b"original-source"):
    source = config.roots["archive"] / relative_path
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(payload)
    return source


def test_full_pipeline_replaces_source_with_verified_output(
    worker_config_factory, fake_prober, fake_validator, fake_recover
):
    config = worker_config_factory()
    source = seed_source(config)
    encoder = FakeEncoder(payload=b"encoded-output")
    pipeline = make_pipeline(
        config,
        encode=encoder,
        probe=fake_prober(),
        validate=fake_validator(),
        recover=fake_recover(),
    )

    outcome = pipeline.run(make_job(), stop_event=threading.Event())

    assert outcome.disposition == "complete"
    assert outcome.output is not None
    assert source.read_bytes() == b"encoded-output"
    assert find_artifacts(source).all == ()
    marker = read_marker(source.parent)
    assert marker is not None and "GS010001.MP4" in marker.clips
    assert marker.clips["GS010001.MP4"].output == outcome.output
    # the encode ran on the local scratch copy, never on the archive path
    encoded_source = encoder.calls[0]["source"]
    assert encoded_source != source
    assert encoder.calls[0]["source_bytes"] == b"original-source"
    # scratch is discarded after the job
    assert list(jobs_root(config.scratch_dir).iterdir()) == []


def test_source_is_untouched_when_replacement_begins(
    worker_config_factory, fake_prober, fake_validator, fake_recover
):
    config = worker_config_factory()
    source = seed_source(config)
    original = fingerprint_file(source)
    observed: dict[str, object] = {}

    def spying_replace(output, source_path, **kwargs):
        observed["fingerprint"] = fingerprint_file(source_path)
        observed["bytes"] = source_path.read_bytes()
        raise VerificationError("injected transfer failure")

    pipeline = make_pipeline(
        config,
        encode=FakeEncoder(),
        probe=fake_prober(),
        validate=fake_validator(),
        recover=fake_recover(),
        replace=spying_replace,
    )
    outcome = pipeline.run(make_job(), stop_event=threading.Event())

    assert outcome.disposition == "failed" and "transfer verification" in outcome.detail
    assert observed["fingerprint"] == original
    assert observed["bytes"] == b"original-source"
    assert source.read_bytes() == b"original-source"


def test_stop_mid_encode_requeues_and_discards_scratch(
    worker_config_factory, fake_prober, fake_validator, fake_recover
):
    config = worker_config_factory()
    source = seed_source(config)
    original = fingerprint_file(source)
    pipeline = make_pipeline(
        config,
        encode=FakeEncoder(mode="stopped"),
        probe=fake_prober(),
        validate=fake_validator(),
        recover=fake_recover(),
    )

    outcome = pipeline.run(make_job(), stop_event=threading.Event())

    assert outcome.disposition == "requeue"
    assert "stopped" in outcome.detail
    assert fingerprint_file(source) == original
    assert list(jobs_root(config.scratch_dir).iterdir()) == []


def test_stop_after_validation_requeues_before_transfer(
    worker_config_factory, fake_prober, fake_validator, fake_recover
):
    config = worker_config_factory()
    source = seed_source(config)
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
    assert source.read_bytes() == b"original-source"


def test_resume_after_stop_sweeps_scratch_and_completes(
    worker_config_factory, fake_prober, fake_validator, fake_recover
):
    config = worker_config_factory()
    source = seed_source(config)
    seams = dict(probe=fake_prober(), validate=fake_validator(), recover=fake_recover())

    first = make_pipeline(config, encode=FakeEncoder(mode="stopped"), **seams)
    assert first.run(make_job(), stop_event=threading.Event()).disposition == "requeue"

    # a crashed process can leave a stale tree; the startup sweep discards it
    stale = jobs_root(config.scratch_dir) / "stale-job"
    stale.mkdir(parents=True)
    (stale / "source").write_bytes(b"stale")
    prepare_scratch_root(config.scratch_dir)
    assert list(jobs_root(config.scratch_dir).iterdir()) == []

    second = make_pipeline(config, encode=FakeEncoder(payload=b"encoded-output"), **seams)
    outcome = second.run(make_job(), stop_event=threading.Event())

    assert outcome.disposition == "complete"
    assert source.read_bytes() == b"encoded-output"


def test_copy_verification_mismatch_is_a_failure(
    worker_config_factory, fake_prober, fake_validator, fake_recover
):
    config = worker_config_factory()
    seed_source(config)
    calls = {"count": 0}
    real_fingerprint = fingerprint_file

    def flaky_fingerprint(path):
        calls["count"] += 1
        current = real_fingerprint(path)
        if calls["count"] == 2:  # the scratch copy
            return Fingerprint(
                size_bytes=current.size_bytes, mtime_ns=current.mtime_ns, digest="corrupted"
            )
        return current

    pipeline = make_pipeline(
        config,
        encode=FakeEncoder(),
        probe=fake_prober(),
        validate=fake_validator(),
        recover=fake_recover(),
        fingerprint=flaky_fingerprint,
    )
    outcome = pipeline.run(make_job(), stop_event=threading.Event())

    assert outcome.disposition == "failed" and "copy verification" in outcome.detail


def test_each_job_resolves_under_its_own_root(
    tmp_path, worker_config_factory, fake_prober, fake_validator, fake_recover
):
    other = tmp_path / "other"
    other.mkdir()
    config = worker_config_factory(
        roots={"archive": tmp_path / "archive", "other": other}
    )
    archive_source = seed_source(config, payload=b"original-archive")
    other_source = config.roots["other"] / CLIP
    other_source.parent.mkdir(parents=True)
    other_source.write_bytes(b"original-other")
    seams = dict(
        encode=FakeEncoder(payload=b"encoded-output"),
        probe=fake_prober(),
        validate=fake_validator(),
        recover=fake_recover(),
    )

    for root_id in ("archive", "other"):
        outcome = make_pipeline(config, **seams).run(
            make_job(root_id=root_id), stop_event=threading.Event()
        )
        assert outcome.disposition == "complete"

    assert archive_source.read_bytes() == b"encoded-output"
    assert other_source.read_bytes() == b"encoded-output"


def test_unexpected_replacement_errors_do_not_escape(
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
        replace=raiser(OSError("disk went away")),
    )
    outcome = pipeline.run(make_job(), stop_event=threading.Event())

    assert outcome.disposition == "failed" and "I/O error" in outcome.detail
