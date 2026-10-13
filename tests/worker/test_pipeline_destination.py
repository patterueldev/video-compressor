"""Destination-mode pipeline flow: placement routing and verified delete (issue #40).

Fast pipeline-level coverage with fake seams: keep-originals and delete-original
placement, the ordering proof that no source is deleted before its output
validated, failure-path source safety, same-folder/legacy routing, and the
destination spec resolution.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path

from fixtures.worker import CLIP, FakeEncoder, make_job
from videocompressor.core.fingerprint import fingerprint_file
from videocompressor.core.place import temp_path_for
from videocompressor.core.records import (
    DestinationRecord,
    meta_path_for,
    read_record,
    write_record,
)
from videocompressor.core.replace import ReplaceResult
from videocompressor.core.validate import ValidationResult
from videocompressor.worker.pipeline import JobPipeline, PipelineSeams
from videocompressor.worker.scratch import jobs_root

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


def test_keep_originals_places_the_output_and_leaves_the_source_alone(
    tmp_path, worker_config_factory, fake_prober, fake_validator, fake_recover
):
    config = worker_config_factory()
    source = seed_source(config)
    original = fingerprint_file(source)
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    pipeline = make_pipeline(
        config,
        encode=FakeEncoder(payload=PLACED),
        probe=fake_prober(),
        validate=fake_validator(),
        recover=fake_recover(),
    )

    outcome = run(pipeline, make_job(destination=custom_folder(outputs), delete_original=False))

    placed = outputs / OUTPUT_NAME
    assert outcome.disposition == "complete" and outcome.skipped is False
    assert outcome.destination == placed
    assert outcome.output == fingerprint_file(placed)
    assert placed.read_bytes() == PLACED
    assert read_record(placed) is not None
    assert meta_path_for(placed).is_file()
    assert not (outputs / f"{OUTPUT_NAME}.vc-meta.json").exists()
    assert source.read_bytes() == ORIGINAL and fingerprint_file(source) == original
    assert not (source.parent / "manifest.json").exists()  # no source-folder marker
    assert list(jobs_root(config.scratch_dir).iterdir()) == []


def test_delete_original_deletes_only_after_the_output_validated(
    tmp_path, worker_config_factory, fake_prober, fake_recover
):
    config = worker_config_factory()
    source = seed_source(config)
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    seen: dict[str, object] = {"source_present": True}
    source_path = source

    def spying_validate(output, settings, *, source=None, **kwargs):
        seen["source_present"] = seen["source_present"] and source_path.exists()
        seen["last_validated"] = Path(output)
        return ValidationResult(ok=True, failures=(), output=None)

    pipeline = make_pipeline(
        config,
        encode=FakeEncoder(payload=PLACED),
        probe=fake_prober(),
        validate=spying_validate,
        recover=fake_recover(),
    )

    outcome = run(pipeline, make_job(destination=custom_folder(outputs), delete_original=True))

    assert outcome.disposition == "complete"
    assert seen["source_present"] is True  # never deleted before validation ran
    validated = seen["last_validated"]
    assert validated.parent == outputs and ".vc-tmp." in validated.name
    assert not source.exists()
    assert (outputs / OUTPUT_NAME).read_bytes() == PLACED
    assert read_record(outputs / OUTPUT_NAME) is not None


def test_delete_original_keeps_the_source_when_destination_validation_fails(
    tmp_path, worker_config_factory, fake_prober, fake_recover
):
    config = worker_config_factory()
    source = seed_source(config)
    outputs = tmp_path / "outputs"
    outputs.mkdir()

    def validate(output, settings, *, source=None, **kwargs):
        ok = Path(output).parent.name != "outputs"  # only the destination copy fails
        return ValidationResult(ok=ok, failures=() if ok else ("decode: broken",), output=None)

    pipeline = make_pipeline(
        config,
        encode=FakeEncoder(payload=PLACED),
        probe=fake_prober(),
        validate=validate,
        recover=fake_recover(),
    )

    outcome = run(pipeline, make_job(destination=custom_folder(outputs), delete_original=True))

    assert outcome.disposition == "failed" and "destination placement failed" in outcome.detail
    assert source.read_bytes() == ORIGINAL
    assert not (outputs / OUTPUT_NAME).exists()
    assert not meta_path_for(outputs / OUTPUT_NAME).exists()


def test_delete_original_keeps_the_source_when_the_placement_copy_fails(
    tmp_path, worker_config_factory, fake_prober, fake_validator, fake_recover, monkeypatch
):
    config = worker_config_factory()
    source = seed_source(config)
    outputs = tmp_path / "outputs"
    outputs.mkdir()

    def corrupt_copy(origin: Path, destination: Path, stop_event, **kwargs) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"corrupted")

    monkeypatch.setattr("videocompressor.worker.destination.copy_with_stop", corrupt_copy)
    pipeline = make_pipeline(
        config,
        encode=FakeEncoder(payload=PLACED),
        probe=fake_prober(),
        validate=fake_validator(),
        recover=fake_recover(),
    )

    outcome = run(pipeline, make_job(destination=custom_folder(outputs), delete_original=True))

    assert outcome.disposition == "failed" and "placement failed" in outcome.detail
    assert source.read_bytes() == ORIGINAL
    assert not (outputs / OUTPUT_NAME).exists()


def test_delete_original_keeps_the_source_when_the_record_write_fails(
    tmp_path, worker_config_factory, fake_prober, fake_validator, fake_recover, monkeypatch
):
    config = worker_config_factory()
    source = seed_source(config)
    outputs = tmp_path / "outputs"
    outputs.mkdir()

    def broken_write(output_path: Path, record: DestinationRecord) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr("videocompressor.core.place.write_record", broken_write)
    pipeline = make_pipeline(
        config,
        encode=FakeEncoder(payload=PLACED),
        probe=fake_prober(),
        validate=fake_validator(),
        recover=fake_recover(),
    )

    outcome = run(pipeline, make_job(destination=custom_folder(outputs), delete_original=True))

    assert outcome.disposition == "failed"
    assert source.read_bytes() == ORIGINAL
    assert not (outputs / OUTPUT_NAME).exists()


def test_source_changed_before_delete_is_review_and_never_deletes(
    tmp_path, worker_config_factory, fake_prober, fake_recover
):
    config = worker_config_factory()
    source = seed_source(config)
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    changed = b"changed-elsewhere"

    def validate(output, settings, *, source=None, **kwargs):
        if Path(output).parent.name == "outputs":
            source_path.write_bytes(changed)
        return ValidationResult(ok=True, failures=(), output=None)

    source_path = source
    pipeline = make_pipeline(
        config,
        encode=FakeEncoder(payload=PLACED),
        probe=fake_prober(),
        validate=validate,
        recover=fake_recover(),
    )

    outcome = run(pipeline, make_job(destination=custom_folder(outputs), delete_original=True))

    assert outcome.disposition == "review" and "source was not deleted" in outcome.detail
    assert source.read_bytes() == changed  # the changed file survives
    assert (outputs / OUTPUT_NAME).read_bytes() == PLACED


def test_same_folder_with_keep_originals_is_refused(
    tmp_path, worker_config_factory, fake_prober, fake_validator, fake_recover
):
    config = worker_config_factory()
    source = seed_source(config)
    encoder = FakeEncoder(payload=PLACED)
    replace_calls: list[tuple] = []
    pipeline = make_pipeline(
        config,
        encode=encoder,
        probe=fake_prober(),
        validate=fake_validator(),
        recover=fake_recover(),
        replace=lambda *args, **kwargs: replace_calls.append(args),
    )

    outcome = run(pipeline, make_job(destination={"kind": "same_folder"}, delete_original=False))

    assert outcome.disposition == "failed" and "delete_original=false" in outcome.detail
    assert encoder.calls == [] and replace_calls == []
    assert source.read_bytes() == ORIGINAL


def test_legacy_job_without_destination_uses_the_verified_swap(
    tmp_path, worker_config_factory, fake_prober, fake_validator, fake_recover
):
    config = worker_config_factory()
    source = seed_source(config)
    replace_calls: list[Path] = []

    def replace_spy(output, source_path, **kwargs):
        replace_calls.append(source_path)

        return ReplaceResult(
            source=source_path, job_id=kwargs["job_id"], output=fingerprint_file(output)
        )

    pipeline = make_pipeline(
        config,
        encode=FakeEncoder(payload=PLACED),
        probe=fake_prober(),
        validate=fake_validator(),
        recover=fake_recover(),
        replace=replace_spy,
    )

    outcome = run(pipeline)

    assert outcome.disposition == "complete"
    assert replace_calls == [source]


def test_destination_resolving_to_the_source_path_uses_the_swap(
    tmp_path, worker_config_factory, fake_prober, fake_validator, fake_recover
):
    config = worker_config_factory()
    source = seed_source(config)
    replace_calls: list[Path] = []

    def replace_spy(output, source_path, **kwargs):
        replace_calls.append(source_path)

        return ReplaceResult(
            source=source_path, job_id=kwargs["job_id"], output=fingerprint_file(output)
        )

    pipeline = make_pipeline(
        config,
        encode=FakeEncoder(payload=PLACED),
        probe=fake_prober(),
        validate=fake_validator(),
        recover=fake_recover(),
        replace=replace_spy,
    )

    outcome = run(
        pipeline, make_job(destination=custom_folder(source.parent), delete_original=True)
    )

    assert outcome.disposition == "complete" and replace_calls == [source]


def test_missing_destination_folder_is_failed_without_touching_the_source(
    tmp_path, worker_config_factory, fake_prober, fake_validator, fake_recover
):
    config = worker_config_factory()
    source = seed_source(config)
    encoder = FakeEncoder(payload=PLACED)
    pipeline = make_pipeline(
        config,
        encode=encoder,
        probe=fake_prober(),
        validate=fake_validator(),
        recover=fake_recover(),
    )

    outcome = run(
        pipeline,
        make_job(destination=custom_folder(tmp_path / "nowhere"), delete_original=True),
    )

    assert outcome.disposition == "failed" and "does not exist on this node" in outcome.detail
    assert encoder.calls == []
    assert source.read_bytes() == ORIGINAL


def test_leftover_destination_artifacts_are_reaped_before_placing(
    tmp_path, worker_config_factory, fake_prober, fake_validator, fake_recover
):
    config = worker_config_factory()
    seed_source(config)
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    leftover = temp_path_for(outputs / OUTPUT_NAME, "old-attempt")
    leftover.write_bytes(b"stale staged bytes")
    pipeline = make_pipeline(
        config,
        encode=FakeEncoder(payload=PLACED),
        probe=fake_prober(),
        validate=fake_validator(),
        recover=fake_recover(),
    )

    outcome = run(pipeline, make_job(destination=custom_folder(outputs), delete_original=False))

    assert outcome.disposition == "complete"
    assert not leftover.exists()
    assert (outputs / OUTPUT_NAME).read_bytes() == PLACED
    assert meta_path_for(outputs / OUTPUT_NAME).exists()


def test_partial_records_inside_the_records_folder_are_reaped_before_placing(
    tmp_path, worker_config_factory, fake_prober, fake_validator, fake_recover
):
    config = worker_config_factory()
    seed_source(config)
    outputs = tmp_path / "outputs"
    records = outputs / ".vc-records"
    records.mkdir(parents=True)
    partial = records / f".{OUTPUT_NAME}.vc-meta.json.stale.tmp"
    partial.write_bytes(b"partial record")
    pipeline = make_pipeline(
        config,
        encode=FakeEncoder(payload=PLACED),
        probe=fake_prober(),
        validate=fake_validator(),
        recover=fake_recover(),
    )

    outcome = run(pipeline, make_job(destination=custom_folder(outputs), delete_original=False))

    assert outcome.disposition == "complete"
    assert not partial.exists()
    assert meta_path_for(outputs / OUTPUT_NAME).is_file()


def test_delete_flag_absent_on_a_custom_destination_keeps_the_source(
    tmp_path, worker_config_factory, fake_prober, fake_validator, fake_recover
):
    config = worker_config_factory()
    source = seed_source(config)
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    pipeline = make_pipeline(
        config,
        encode=FakeEncoder(payload=PLACED),
        probe=fake_prober(),
        validate=fake_validator(),
        recover=fake_recover(),
    )

    outcome = run(pipeline, make_job(destination=custom_folder(outputs)))

    assert outcome.disposition == "complete" and source.read_bytes() == ORIGINAL


def test_pilot_mode_wins_over_a_job_destination(
    tmp_path, worker_config_factory, fake_prober, fake_validator, fake_recover
):
    pilot = tmp_path / "pilot"
    config = worker_config_factory(pilot_root=pilot)
    source = seed_source(config)
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    pipeline = make_pipeline(
        config,
        encode=FakeEncoder(payload=PLACED),
        probe=fake_prober(),
        validate=fake_validator(),
        recover=fake_recover(),
    )

    outcome = run(
        pipeline, make_job(destination=custom_folder(outputs), delete_original=True)
    )

    assert outcome.pilot is True and outcome.disposition == "review"
    assert (pilot / CLIP).read_bytes() == PLACED
    assert not (outputs / OUTPUT_NAME).exists()
    assert source.read_bytes() == ORIGINAL
