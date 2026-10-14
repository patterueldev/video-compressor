"""Output name patterns at the worker: placement and rendering (issue #87).

The claim's naming spec decides the output file name used by placement, skip,
resume, and collision checks. The subfolder tests cover the placement fix: an
explicit output-root ``relative_path`` is a folder, so two clips in the same
subfolder both place instead of colliding in ``review``. Skip, collision,
resume, and delete ordering live in ``test_pipeline_naming_flow.py``.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path

from fixtures.worker import CLIP, FakeEncoder, make_job
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


def custom_folder(path: Path) -> dict:
    return {"kind": "custom_folder", "path": str(path)}


def output_roots(tmp_path) -> dict[str, Path]:
    return {"archive": tmp_path / "archive", "outputs": tmp_path / "outputs"}


def make_fake_pipeline(config, fake_prober, fake_validator, fake_recover):
    return make_pipeline(
        config,
        encode=FakeEncoder(payload=PLACED),
        probe=fake_prober(),
        validate=fake_validator(),
        recover=fake_recover(),
    )


# -- the subfolder placement fix ----------------------------------------------


def test_an_explicit_subfolder_is_a_folder_not_a_file(
    tmp_path, worker_config_factory, fake_prober, fake_validator, fake_recover
):
    config = worker_config_factory(roots=output_roots(tmp_path))
    source = seed_source(config)
    pipeline = make_fake_pipeline(config, fake_prober, fake_validator, fake_recover)

    outcome = run(
        pipeline,
        make_job(destination=output_root("outputs", "2026-10"), delete_original=False),
    )

    subfolder = tmp_path / "outputs" / "2026-10"
    assert subfolder.is_dir()
    assert outcome.disposition == "complete"
    assert outcome.destination == subfolder / "GS010001.MP4"
    assert (subfolder / "GS010001.MP4").read_bytes() == PLACED
    assert source.read_bytes() == ORIGINAL


def test_two_clips_in_the_same_subfolder_both_place(
    tmp_path, worker_config_factory, fake_prober, fake_validator, fake_recover
):
    config = worker_config_factory(roots=output_roots(tmp_path))
    first = seed_source(config, CLIP)
    second = seed_source(config, "2024-06-Ride/GS010002.MP4")
    pipeline = make_fake_pipeline(config, fake_prober, fake_validator, fake_recover)
    destination = output_root("outputs", "2026-10")

    first_outcome = run(pipeline, make_job(destination=destination, delete_original=False))
    second_outcome = run(
        pipeline,
        make_job("2024-06-Ride/GS010002.MP4", destination=destination, delete_original=False),
    )

    assert first_outcome.disposition == "complete"
    assert second_outcome.disposition == "complete"
    subfolder = tmp_path / "outputs" / "2026-10"
    placed = sorted(entry.name for entry in subfolder.iterdir() if entry.is_file())
    assert placed == ["GS010001.MP4", "GS010002.MP4"]
    assert first.read_bytes() == ORIGINAL and second.read_bytes() == ORIGINAL


def test_the_mirror_form_keeps_the_source_relative_path(
    tmp_path, worker_config_factory, fake_prober, fake_validator, fake_recover
):
    config = worker_config_factory(roots=output_roots(tmp_path))
    source = seed_source(config)
    pipeline = make_fake_pipeline(config, fake_prober, fake_validator, fake_recover)

    outcome = run(pipeline, make_job(destination=output_root("outputs"), delete_original=False))

    placed = tmp_path / "outputs" / CLIP
    assert outcome.destination == placed
    assert placed.read_bytes() == PLACED
    assert source.read_bytes() == ORIGINAL


def test_the_mirror_form_of_a_root_level_source_uses_the_root_itself(
    tmp_path, worker_config_factory, fake_prober, fake_validator, fake_recover
):
    config = worker_config_factory(roots=output_roots(tmp_path))
    seed_source(config, "GS010001.MP4")
    pipeline = make_fake_pipeline(config, fake_prober, fake_validator, fake_recover)

    outcome = run(
        pipeline,
        make_job("GS010001.MP4", destination=output_root("outputs"), delete_original=False),
    )

    assert outcome.destination == tmp_path / "outputs" / "GS010001.MP4"
    assert outcome.destination.read_bytes() == PLACED


# -- naming resolution ---------------------------------------------------------


def test_a_claim_without_naming_keeps_the_source_name(
    tmp_path, worker_config_factory, fake_prober, fake_validator, fake_recover
):
    # the fake probe reports no creation_time: the default pattern needs none
    config = worker_config_factory(roots=output_roots(tmp_path))
    seed_source(config)
    pipeline = make_fake_pipeline(config, fake_prober, fake_validator, fake_recover)

    outcome = run(
        pipeline,
        make_job(destination=output_root("outputs", "2026-10"), delete_original=False),
    )

    assert outcome.destination == tmp_path / "outputs" / "2026-10" / "GS010001.MP4"


def test_a_default_pattern_payload_is_ignored(
    tmp_path, worker_config_factory, fake_prober, fake_validator, fake_recover
):
    config = worker_config_factory(roots=output_roots(tmp_path))
    seed_source(config)
    pipeline = make_fake_pipeline(config, fake_prober, fake_validator, fake_recover)

    outcome = run(
        pipeline,
        make_job(
            destination=output_root("outputs", "2026-10"),
            naming=naming("{name}"),
            delete_original=False,
        ),
    )

    assert outcome.destination == tmp_path / "outputs" / "2026-10" / "GS010001.MP4"


def test_a_pattern_uses_the_claims_capture_time(
    tmp_path, worker_config_factory, fake_prober, fake_validator, fake_recover
):
    config = worker_config_factory(roots=output_roots(tmp_path))
    seed_source(config)
    pipeline = make_fake_pipeline(config, fake_prober, fake_validator, fake_recover)

    outcome = run(
        pipeline,
        make_job(
            destination=output_root("outputs", "2026-10"),
            capture_time=CLAIM_TIME,
            naming=naming("{date}_{name}"),
            delete_original=False,
        ),
    )

    assert outcome.destination == tmp_path / "outputs" / "2026-10" / RENDERED


def test_a_custom_datetime_format_is_rendered(
    tmp_path, worker_config_factory, fake_prober, fake_validator, fake_recover
):
    config = worker_config_factory(roots=output_roots(tmp_path))
    seed_source(config)
    pipeline = make_fake_pipeline(config, fake_prober, fake_validator, fake_recover)

    outcome = run(
        pipeline,
        make_job(
            destination=output_root("outputs", "2026-10"),
            capture_time=CLAIM_TIME,
            naming=naming("{datetime:%Y%m%d%H%M}_{name}"),
            delete_original=False,
        ),
    )

    assert outcome.destination == tmp_path / "outputs" / "2026-10" / "202504192117_GS010001.MP4"


def test_a_custom_folder_gets_the_rendered_name(
    tmp_path, worker_config_factory, fake_prober, fake_validator, fake_recover
):
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    config = worker_config_factory()
    seed_source(config)
    pipeline = make_fake_pipeline(config, fake_prober, fake_validator, fake_recover)

    outcome = run(
        pipeline,
        make_job(
            destination=custom_folder(outputs),
            capture_time=CLAIM_TIME,
            naming=naming("{date}_{name}"),
            delete_original=False,
        ),
    )

    assert outcome.destination == outputs / RENDERED
    assert (outputs / RENDERED).read_bytes() == PLACED


def test_a_pattern_applies_to_the_mirror_form(
    tmp_path, worker_config_factory, fake_prober, fake_validator, fake_recover
):
    config = worker_config_factory(roots=output_roots(tmp_path))
    seed_source(config)
    pipeline = make_fake_pipeline(config, fake_prober, fake_validator, fake_recover)

    outcome = run(
        pipeline,
        make_job(
            destination=output_root("outputs"),
            capture_time=CLAIM_TIME,
            naming=naming("{date}_{name}"),
            delete_original=False,
        ),
    )

    assert outcome.destination == tmp_path / "outputs" / "2024-06-Ride" / RENDERED


def test_a_pattern_falls_back_to_the_sources_raw_creation_time(
    tmp_path, worker_config_factory, fake_media_factory, fake_validator, fake_recover
):
    config = worker_config_factory(roots=output_roots(tmp_path))
    seed_source(config)
    media = replace(fake_media_factory(), format_tags={"creation_time": "2025-04-19T21:17:05Z"})
    pipeline = make_pipeline(
        config,
        encode=FakeEncoder(payload=PLACED),
        probe=lambda path: media,
        validate=fake_validator(),
        recover=fake_recover(),
    )

    outcome = run(
        pipeline,
        make_job(
            destination=output_root("outputs", "2026-10"),
            naming=naming("{date}_{name}"),
            delete_original=False,
        ),
    )

    assert outcome.destination == tmp_path / "outputs" / "2026-10" / RENDERED


def test_the_fallback_parses_under_the_claims_capture_mode(
    tmp_path, worker_config_factory, fake_media_factory, fake_validator, fake_recover
):
    config = worker_config_factory(roots=output_roots(tmp_path))
    seed_source(config)
    media = replace(
        fake_media_factory(), format_tags={"creation_time": "2025-04-19T21:17:05+00:00"}
    )
    pipeline = make_pipeline(
        config,
        encode=FakeEncoder(payload=PLACED),
        probe=lambda path: media,
        validate=fake_validator(),
        recover=fake_recover(),
    )
    destination = output_root("outputs", "2026-10")

    local = run(
        pipeline,
        make_job(destination=destination, naming=naming("{date}_{name}"), delete_original=False),
    )
    utc = run(
        pipeline,
        make_job(
            destination=destination,
            naming=naming("{date}_{name}", capture_mode="utc"),
            delete_original=False,
        ),
    )

    assert local.destination == tmp_path / "outputs" / "2026-10" / RENDERED
    assert utc.destination == tmp_path / "outputs" / "2026-10" / "20250420_GS010001.MP4"


def test_a_pattern_needing_a_capture_time_fails_when_none_is_found(
    tmp_path, worker_config_factory, fake_prober, fake_validator, fake_recover
):
    config = worker_config_factory(roots=output_roots(tmp_path))
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
        make_job(
            destination=output_root("outputs", "2026-10"),
            naming=naming("{date}_{name}"),
            delete_original=False,
        ),
    )

    assert outcome.disposition == "failed"
    assert "name pattern needs a capture time" in outcome.detail
    assert encoder.calls == []
    assert source.read_bytes() == ORIGINAL
