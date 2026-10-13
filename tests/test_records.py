"""Record storage location and legacy fallback (issue #78).

Fast unit coverage: records live in the hidden ``.vc-records/`` folder inside
the destination directory, writes create the folder atomically and leave no
beside-file record, and reads still honour a legacy record written beside the
output by an older version (no forced migration).
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

from videocompressor.core.records import (
    META_SUFFIX,
    RECORDS_DIR,
    DestinationRecord,
    legacy_meta_path_for,
    meta_path_for,
    read_record,
    write_record,
)

CLIP = "GS010001.MP4"
RELATIVE = "2024-06-Ride/GS010001.MP4"
ORIGINAL = b"original GoPro bytes" * 32
OUTPUT = b"compressed output bytes" * 24


def _destination(tmp_path: Path) -> tuple[Path, Path]:
    """Return (source, destination output) with both files on disk."""
    source = tmp_path / "archive" / CLIP
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(ORIGINAL)
    destination = tmp_path / "out" / CLIP
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(OUTPUT)
    return source, destination


def _record(source: Path, destination: Path, fingerprint, **overrides) -> DestinationRecord:
    return DestinationRecord(
        root_id="archive",
        source_relative_path=RELATIVE,
        clip=destination.name,
        source=fingerprint(source),
        output=fingerprint(destination),
        completed_at=datetime.now(UTC),
        **overrides,
    )


def test_meta_path_for_uses_the_hidden_records_folder(tmp_path):
    destination = tmp_path / "out" / CLIP

    assert RECORDS_DIR == ".vc-records"
    assert META_SUFFIX == ".vc-meta.json"
    assert meta_path_for(destination) == (
        destination.parent / ".vc-records" / f"{CLIP}.vc-meta.json"
    )
    assert legacy_meta_path_for(destination) == destination.parent / f"{CLIP}.vc-meta.json"


def test_write_record_creates_the_folder_and_leaves_no_beside_file(tmp_path, fingerprint_full):
    source, destination = _destination(tmp_path)
    record = _record(source, destination, fingerprint_full)

    path = write_record(destination, record)

    assert path == meta_path_for(destination)
    assert path.is_file() and path.parent.name == RECORDS_DIR
    assert read_record(destination) == record
    assert not legacy_meta_path_for(destination).exists()
    assert not any(entry.name.endswith(META_SUFFIX) for entry in destination.parent.iterdir())


def test_a_legacy_beside_file_record_still_reads(tmp_path, fingerprint_full):
    source, destination = _destination(tmp_path)
    record = _record(source, destination, fingerprint_full)
    shutil.move(write_record(destination, record), legacy_meta_path_for(destination))

    assert not meta_path_for(destination).exists()
    assert read_record(destination) == record


def test_the_records_folder_record_wins_over_a_legacy_one(tmp_path, fingerprint_full):
    source, destination = _destination(tmp_path)
    legacy_record = _record(source, destination, fingerprint_full, template_id="legacy")
    shutil.move(write_record(destination, legacy_record), legacy_meta_path_for(destination))
    new_record = _record(source, destination, fingerprint_full, template_id="new")
    write_record(destination, new_record)

    assert read_record(destination) == new_record


def test_a_corrupt_new_record_falls_back_to_the_legacy_one(tmp_path, fingerprint_full):
    source, destination = _destination(tmp_path)
    record = _record(source, destination, fingerprint_full)
    shutil.move(write_record(destination, record), legacy_meta_path_for(destination))

    meta_path_for(destination).write_text("{not json", encoding="utf-8")
    assert read_record(destination) == record

    meta_path_for(destination).write_text('{"version": 99}', encoding="utf-8")
    assert read_record(destination) == record

    legacy_meta_path_for(destination).write_text("{not json", encoding="utf-8")
    assert read_record(destination) is None


def test_read_record_returns_none_without_any_record(tmp_path):
    destination = tmp_path / "out" / CLIP
    destination.parent.mkdir(parents=True, exist_ok=True)

    assert read_record(destination) is None
