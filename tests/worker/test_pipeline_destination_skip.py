"""Destination-mode skip, collision, and resume rules (issue #40).

Fast pipeline-level coverage: a valid existing output for the same source
fingerprint is skipped without re-encoding (and the source is deleted only when
the job asked for it), anything else at the destination is a collision, and a
missing source with a matching record resumes as skipped.
"""

from __future__ import annotations

import shutil
import threading
from datetime import UTC, datetime
from pathlib import Path

from fixtures.worker import CLIP, FakeEncoder, make_job
from videocompressor.core.fingerprint import fingerprint_file
from videocompressor.core.records import (
    DestinationRecord,
    legacy_meta_path_for,
    meta_path_for,
    write_record,
)
from videocompressor.worker.pipeline import JobPipeline, PipelineSeams

OUTPUT_NAME = Path(CLIP).name
ORIGINAL = b"original-source"
PLACED = b"placed-output"


def make_pipeline(config, **seams) -> JobPipeline:
    return JobPipeline(config, seams=PipelineSeams(**seams))


def seed_source(config, payload: bytes = ORIGINAL) -> Path:
    source = config.roots["archive"] / CLIP
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(payload)
    return source


def run(pipeline, job=None, stop=None):
    stop_event = stop or threading.Event()
    return pipeline.run(job if job is not None else make_job(), stop_event=stop_event)


def custom_folder(path: Path) -> dict:
    return {"kind": "custom_folder", "path": str(path)}


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


def test_skip_keeps_originals_and_does_not_encode(
    tmp_path, worker_config_factory, fake_prober, fake_validator, fake_recover
):
    config = worker_config_factory()
    source = seed_source(config)
    outputs = tmp_path / "outputs"
    placed = outputs / OUTPUT_NAME
    seed_placed(placed, source)
    encoder = FakeEncoder(payload=PLACED)
    pipeline = make_pipeline(
        config,
        encode=encoder,
        probe=fake_prober(),
        validate=fake_validator(),
        recover=fake_recover(),
    )

    outcome = run(pipeline, make_job(destination=custom_folder(outputs), delete_original=False))

    assert outcome.disposition == "complete" and outcome.skipped is True
    assert "skipped" in outcome.detail
    assert encoder.calls == []
    assert placed.read_bytes() == PLACED
    assert source.read_bytes() == ORIGINAL


def test_skip_with_delete_original_deletes_the_source(
    tmp_path, worker_config_factory, fake_prober, fake_validator, fake_recover
):
    config = worker_config_factory()
    source = seed_source(config)
    outputs = tmp_path / "outputs"
    placed = outputs / OUTPUT_NAME
    seed_placed(placed, source)
    encoder = FakeEncoder(payload=PLACED)
    pipeline = make_pipeline(
        config,
        encode=encoder,
        probe=fake_prober(),
        validate=fake_validator(),
        recover=fake_recover(),
    )

    outcome = run(pipeline, make_job(destination=custom_folder(outputs), delete_original=True))

    assert outcome.disposition == "complete" and outcome.skipped is True
    assert not source.exists()
    assert encoder.calls == []


def test_a_changed_source_makes_the_existing_output_a_collision(
    tmp_path, worker_config_factory, fake_prober, fake_validator, fake_recover
):
    config = worker_config_factory()
    source = seed_source(config)
    outputs = tmp_path / "outputs"
    placed = outputs / OUTPUT_NAME
    seed_placed(placed, source)
    source.write_bytes(b"changed-elsewhere")
    encoder = FakeEncoder(payload=PLACED)
    pipeline = make_pipeline(
        config,
        encode=encoder,
        probe=fake_prober(),
        validate=fake_validator(),
        recover=fake_recover(),
    )

    outcome = run(pipeline, make_job(destination=custom_folder(outputs), delete_original=False))

    assert outcome.disposition == "review" and "refusing to overwrite" in outcome.detail
    assert encoder.calls == []
    assert placed.read_bytes() == PLACED
    assert source.read_bytes() == b"changed-elsewhere"


def test_an_unrecorded_destination_file_is_a_collision(
    tmp_path, worker_config_factory, fake_prober, fake_validator, fake_recover
):
    config = worker_config_factory()
    seed_source(config)
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    placed = outputs / OUTPUT_NAME
    placed.write_bytes(b"someone else's file")
    pipeline = make_pipeline(
        config,
        encode=FakeEncoder(payload=PLACED),
        probe=fake_prober(),
        validate=fake_validator(),
        recover=fake_recover(),
    )

    outcome = run(pipeline, make_job(destination=custom_folder(outputs), delete_original=False))

    assert outcome.disposition == "review"
    assert placed.read_bytes() == b"someone else's file"


def test_source_missing_with_a_valid_record_resumes_as_skipped(
    tmp_path, worker_config_factory, fake_prober, fake_validator
):
    config = worker_config_factory()
    source = seed_source(config)
    outputs = tmp_path / "outputs"
    placed = outputs / OUTPUT_NAME
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

    outcome = run(pipeline, make_job(destination=custom_folder(outputs), delete_original=True))

    assert outcome.disposition == "complete" and outcome.skipped is True
    assert "source is gone" in outcome.detail
    assert encoder.calls == []
    assert placed.read_bytes() == PLACED


def test_a_legacy_beside_file_record_still_skips(
    tmp_path, worker_config_factory, fake_prober, fake_validator, fake_recover
):
    config = worker_config_factory()
    source = seed_source(config)
    outputs = tmp_path / "outputs"
    placed = outputs / OUTPUT_NAME
    seed_placed(placed, source)
    shutil.move(meta_path_for(placed), legacy_meta_path_for(placed))
    encoder = FakeEncoder(payload=PLACED)
    pipeline = make_pipeline(
        config,
        encode=encoder,
        probe=fake_prober(),
        validate=fake_validator(),
        recover=fake_recover(),
    )

    outcome = run(pipeline, make_job(destination=custom_folder(outputs), delete_original=False))

    assert outcome.disposition == "complete" and outcome.skipped is True
    assert encoder.calls == []
    assert placed.read_bytes() == PLACED
    assert source.read_bytes() == ORIGINAL


def test_source_missing_resume_reads_a_legacy_record(
    tmp_path, worker_config_factory, fake_prober, fake_validator
):
    config = worker_config_factory()
    source = seed_source(config)
    outputs = tmp_path / "outputs"
    placed = outputs / OUTPUT_NAME
    seed_placed(placed, source)
    shutil.move(meta_path_for(placed), legacy_meta_path_for(placed))
    source.unlink()
    encoder = FakeEncoder(payload=PLACED)
    pipeline = make_pipeline(
        config,
        encode=encoder,
        probe=fake_prober(),
        validate=fake_validator(),
        readable=lambda path: True,
    )

    outcome = run(pipeline, make_job(destination=custom_folder(outputs), delete_original=True))

    assert outcome.disposition == "complete" and outcome.skipped is True
    assert "source is gone" in outcome.detail
    assert encoder.calls == []
    assert placed.read_bytes() == PLACED


def test_source_missing_without_a_record_is_review(
    tmp_path, worker_config_factory, fake_prober, fake_validator
):
    config = worker_config_factory()
    source = seed_source(config)
    source.unlink()
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    pipeline = make_pipeline(
        config,
        encode=FakeEncoder(payload=PLACED),
        probe=fake_prober(),
        validate=fake_validator(),
        readable=lambda path: True,
    )

    outcome = run(pipeline, make_job(destination=custom_folder(outputs), delete_original=True))

    assert outcome.disposition == "review" and "recovery" in outcome.detail


def test_output_root_resolves_through_worker_roots(
    tmp_path, worker_config_factory, fake_prober, fake_validator, fake_recover
):
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    config = worker_config_factory(
        roots={"archive": tmp_path / "archive", "outputs": outputs}
    )
    source = seed_source(config)
    pipeline = make_pipeline(
        config,
        encode=FakeEncoder(payload=PLACED),
        probe=fake_prober(),
        validate=fake_validator(),
        recover=fake_recover(),
    )

    outcome = run(
        pipeline,
        make_job(destination={"kind": "output_root", "root_id": "outputs"}, delete_original=False),
    )

    assert outcome.destination == outputs / CLIP  # the source-relative path carries over
    assert (outputs / CLIP).read_bytes() == PLACED
    assert source.read_bytes() == ORIGINAL


def test_unmapped_or_malformed_destinations_fail_without_touching_the_source(
    tmp_path, worker_config_factory, fake_prober, fake_validator, fake_recover
):
    config = worker_config_factory(
        roots={"archive": tmp_path / "archive", "outputs": tmp_path / "outputs"}
    )
    source = seed_source(config)
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    encoder = FakeEncoder(payload=PLACED)
    pipeline = make_pipeline(
        config,
        encode=encoder,
        probe=fake_prober(),
        validate=fake_validator(),
        recover=fake_recover(),
    )

    cases = [
        {"kind": "output_root", "root_id": "other"},
        {"kind": "custom_folder", "path": "relative/dir"},
        {"kind": "mystery"},
    ]
    for destination in cases:
        outcome = run(pipeline, make_job(destination=destination, delete_original=True))
        assert outcome.disposition == "failed" and "destination" in outcome.detail

    assert encoder.calls == []
    assert source.read_bytes() == ORIGINAL


