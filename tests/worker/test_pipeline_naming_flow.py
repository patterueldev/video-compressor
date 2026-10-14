"""Output name patterns at the worker: skip, collision, resume, delete (issue #87).

A naming spec must be applied consistently: the rendered path is what skip and
collision checks look at, a source-missing resume uses the claim's capture time
to find the placed output, and a source is deleted only after the placed output
validated. Defensive decode of the optional claim field lives here too.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path

from fixtures.worker import CLIP, FakeEncoder, make_job
from videocompressor.core.fingerprint import fingerprint_file
from videocompressor.core.models import CaptureTimeMode
from videocompressor.core.records import DestinationRecord, write_record
from videocompressor.core.validate import ValidationResult
from videocompressor.worker.client import ClaimedJob
from videocompressor.worker.destination_paths import naming_from_job
from videocompressor.worker.pipeline import JobPipeline, PipelineSeams

ORIGINAL = b"original-source"
PLACED = b"placed-output"

TIMEZONE = "Asia/Manila"
CLAIM_TIME = "2025-04-19T21:17:05+08:00"
RENDERED = "20250419_GS010001.MP4"


def make_pipeline(config, **seams) -> JobPipeline:
    return JobPipeline(config, seams=PipelineSeams(**seams))


def seed_source(config, relative: str = CLIP, payload: bytes = ORIGINAL) -> Path:
    source = config.roots["archive"] / relative
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(payload)
    return source


def run(pipeline, job=None):
    return pipeline.run(job if job is not None else make_job(), stop_event=threading.Event())


def naming(
    pattern: str, *, timezone: str = TIMEZONE, capture_mode: str = "local_wall_clock"
) -> dict:
    return {"pattern": pattern, "timezone": timezone, "capture_mode": capture_mode}


def output_root(root_id: str, relative: str | None = None) -> dict:
    spec: dict = {"kind": "output_root", "root_id": root_id}
    if relative is not None:
        spec["relative_path"] = relative
    return spec


def output_roots(tmp_path) -> dict[str, Path]:
    return {"archive": tmp_path / "archive", "outputs": tmp_path / "outputs"}


def rendered_job(**kwargs) -> ClaimedJob:
    return make_job(
        destination=output_root("outputs", "2026-10"),
        capture_time=CLAIM_TIME,
        naming=naming("{date}_{name}"),
        **kwargs,
    )


def seed_placed(destination: Path, source: Path, payload: bytes = PLACED) -> DestinationRecord:
    """Write a placed output and its matching record for the current source."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)
    record = DestinationRecord(
        root_id="archive",
        source_relative_path=CLIP,
        clip=destination.name,
        source=fingerprint_file(source),
        output=fingerprint_file(destination),
        completed_at=datetime.now(UTC),
    )
    write_record(destination, record)
    return record


def test_a_valid_existing_output_at_the_rendered_path_skips(
    tmp_path, worker_config_factory, fake_prober, fake_validator, fake_recover
):
    config = worker_config_factory(roots=output_roots(tmp_path))
    source = seed_source(config)
    placed = tmp_path / "outputs" / "2026-10" / RENDERED
    seed_placed(placed, source)
    encoder = FakeEncoder(payload=PLACED)
    pipeline = make_pipeline(
        config,
        encode=encoder,
        probe=fake_prober(),
        validate=fake_validator(),
        recover=fake_recover(),
    )

    outcome = run(pipeline, rendered_job())

    assert outcome.disposition == "complete" and outcome.skipped is True
    assert outcome.destination == placed
    assert encoder.calls == []
    assert source.read_bytes() == ORIGINAL


def test_an_occupied_rendered_path_needs_review(
    tmp_path, worker_config_factory, fake_prober, fake_validator, fake_recover
):
    config = worker_config_factory(roots=output_roots(tmp_path))
    source = seed_source(config)
    occupied = tmp_path / "outputs" / "2026-10" / RENDERED
    occupied.parent.mkdir(parents=True)
    occupied.write_bytes(b"someone else's file")
    encoder = FakeEncoder(payload=PLACED)
    pipeline = make_pipeline(
        config,
        encode=encoder,
        probe=fake_prober(),
        validate=fake_validator(),
        recover=fake_recover(),
    )

    outcome = run(pipeline, rendered_job())

    assert outcome.disposition == "review" and "refusing to overwrite" in outcome.detail
    assert occupied.read_bytes() == b"someone else's file"
    assert encoder.calls == []
    assert source.read_bytes() == ORIGINAL


def test_source_missing_resume_uses_the_rendered_path(
    tmp_path, worker_config_factory, fake_prober, fake_validator
):
    config = worker_config_factory(roots=output_roots(tmp_path))
    source = seed_source(config)
    placed = tmp_path / "outputs" / "2026-10" / RENDERED
    seed_placed(placed, source)
    source.unlink()
    encoder = FakeEncoder(payload=PLACED)
    pipeline = make_pipeline(
        config,
        encode=encoder,
        probe=fake_prober(),
        validate=fake_validator(),
        readable=lambda path: True,
    )

    outcome = run(pipeline, rendered_job(delete_original=True))

    assert outcome.disposition == "complete" and outcome.skipped is True
    assert "source is gone" in outcome.detail
    assert encoder.calls == []
    assert placed.read_bytes() == PLACED


def test_delete_original_waits_for_validation_with_a_pattern(
    tmp_path, worker_config_factory, fake_prober, fake_recover
):
    config = worker_config_factory(roots=output_roots(tmp_path))
    source = seed_source(config)
    seen: dict[str, object] = {"source_present": True}
    source_path = source

    def spying_validate(output, settings, *, source=None, **kwargs):
        seen["source_present"] = seen["source_present"] and source_path.exists()
        return ValidationResult(ok=True, failures=(), output=None)

    pipeline = make_pipeline(
        config,
        encode=FakeEncoder(payload=PLACED),
        probe=fake_prober(),
        validate=spying_validate,
        recover=fake_recover(),
    )

    outcome = run(pipeline, rendered_job(delete_original=True))

    assert outcome.disposition == "complete"
    assert seen["source_present"] is True  # never deleted before validation ran
    assert not source.exists()
    assert (tmp_path / "outputs" / "2026-10" / RENDERED).read_bytes() == PLACED


def test_naming_from_job_decodes_valid_specs():
    job = ClaimedJob(
        root_id="archive",
        relative_path=CLIP,
        naming={
            "pattern": "{date}_{name}",
            "timezone": TIMEZONE,
            "capture_mode": "utc",
        },
    )

    spec = naming_from_job(job)

    assert spec is not None
    assert spec.pattern == "{date}_{name}"
    assert spec.timezone == TIMEZONE
    assert spec.capture_mode is CaptureTimeMode.UTC


def test_naming_from_job_treats_unusable_specs_as_absent():
    unusable = [
        None,
        "not-an-object",
        {},
        {"pattern": "{name}", "timezone": TIMEZONE, "capture_mode": "utc"},
        {"pattern": "{camera}_{name}", "timezone": TIMEZONE, "capture_mode": "utc"},
        {"pattern": "{date}_{name}", "timezone": "Nowhere/Nowhere", "capture_mode": "utc"},
        {"pattern": 7, "timezone": TIMEZONE},
    ]

    for raw in unusable:
        job = ClaimedJob(root_id="archive", relative_path=CLIP, naming=raw)
        assert naming_from_job(job) is None


def test_an_unknown_capture_mode_falls_back_to_local_wall_clock():
    job = ClaimedJob(
        root_id="archive",
        relative_path=CLIP,
        naming={"pattern": "{date}_{name}", "timezone": TIMEZONE, "capture_mode": "sideways"},
    )

    spec = naming_from_job(job)

    assert spec is not None
    assert spec.capture_mode is CaptureTimeMode.LOCAL_WALL_CLOCK
