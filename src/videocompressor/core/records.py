"""Destination completion records for the convert flow (issue #40).

A record is a small JSON sidecar written into a hidden ``.vc-records/`` folder
inside the destination directory (``.vc-records/<output name>.vc-meta.json``,
issue #78). It ties the destination file to the clip identity and the source
fingerprint captured at claim, so a later attempt can skip a valid existing
output and the source-missing resume can trust the recorded output. Writes are
atomic (temp file + ``os.replace``); reads try the ``.vc-records/`` location
first and fall back to the legacy beside-file location, so records written by
older versions keep working without a forced migration. Reads degrade to
``None`` for missing, corrupt, or unknown-version records.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from videocompressor.core.fingerprint import fingerprint_confirms, fingerprint_file
from videocompressor.core.models import Fingerprint

__all__ = [
    "META_SUFFIX",
    "RECORDS_DIR",
    "RECORD_VERSION",
    "DestinationRecord",
    "ExistingOutput",
    "classify_existing_output",
    "legacy_meta_path_for",
    "meta_path_for",
    "read_record",
    "write_record",
]

#: Suffix of the per-output completion record.
META_SUFFIX = ".vc-meta.json"

#: Hidden folder inside the destination directory that holds the records.
RECORDS_DIR = ".vc-records"

#: Record schema version this code reads and writes.
RECORD_VERSION = 1


@dataclass(frozen=True)
class DestinationRecord:
    """Completion record for one output placed at an explicit destination."""

    root_id: str
    source_relative_path: str
    clip: str
    source: Fingerprint
    output: Fingerprint
    template_id: str | None = None
    settings_hash: str | None = None
    completed_at: datetime | None = None
    version: int = RECORD_VERSION

    def names(self, *, root_id: str, relative_path: str, clip: str) -> bool:
        """Whether this record names the given clip identity."""
        return (
            self.root_id == root_id
            and self.source_relative_path == relative_path
            and self.clip == clip
        )


def meta_path_for(output_path: Path) -> Path:
    """Return the hidden ``.vc-records/`` record path for ``output_path``."""
    return output_path.parent / RECORDS_DIR / (output_path.name + META_SUFFIX)


def legacy_meta_path_for(output_path: Path) -> Path:
    """Return the legacy beside-file record path for ``output_path``."""
    return output_path.with_name(output_path.name + META_SUFFIX)


def read_record(output_path: Path) -> DestinationRecord | None:
    """Read the record for ``output_path``.

    The hidden ``.vc-records/`` location is tried first; a record written
    beside the output by an older version is the fallback. A missing, corrupt,
    or unknown-version record reads as ``None``.
    """
    for path in (meta_path_for(output_path), legacy_meta_path_for(output_path)):
        record = _read_record_file(path)
        if record is not None:
            return record
    return None


def _read_record_file(path: Path) -> DestinationRecord | None:
    try:
        with path.open("rb") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != RECORD_VERSION:
        return None
    root_id = data.get("root_id")
    relative_path = data.get("source_relative_path")
    clip = data.get("clip")
    if not all(isinstance(value, str) and value for value in (root_id, relative_path, clip)):
        return None
    source = _fingerprint_from_json(data.get("source"))
    output = _fingerprint_from_json(data.get("output"))
    if source is None or output is None:
        return None
    return DestinationRecord(
        root_id=root_id,
        source_relative_path=relative_path,
        clip=clip,
        source=source,
        output=output,
        template_id=_optional_str(data.get("template_id")),
        settings_hash=_optional_str(data.get("settings_hash")),
        completed_at=_parse_datetime(data.get("completed_at")),
    )


def write_record(output_path: Path, record: DestinationRecord) -> Path:
    """Atomically write ``record`` into ``.vc-records/`` and return its path."""
    path = meta_path_for(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "version": RECORD_VERSION,
        "clip": record.clip,
        "root_id": record.root_id,
        "source_relative_path": record.source_relative_path,
        "template_id": record.template_id,
        "settings_hash": record.settings_hash,
        "completed_at": (record.completed_at or datetime.now(UTC)).isoformat(),
        "source": _fingerprint_to_json(record.source),
        "output": _fingerprint_to_json(record.output),
    }
    file_descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{output_path.name}{META_SUFFIX}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp_name)
        raise
    return path


def classify_existing_output(
    output_path: Path,
    *,
    root_id: str,
    relative_path: str,
    source_fingerprint: Fingerprint | None,
    validate: Callable[[Path], bool] | None = None,
    fingerprint: Callable[[Path], Fingerprint] = fingerprint_file,
) -> tuple[ExistingOutput, DestinationRecord | None]:
    """Classify an existing destination file for this job.

    ``SKIP`` requires a record that names this clip, a source fingerprint that
    still confirms the claim (checked only when ``source_fingerprint`` is given:
    the source-missing resume has none), an output fingerprint that still confirms
    the file, and — when ``validate`` is passed — a clean full validation.
    Anything else that occupies the destination path is a ``COLLISION``: the
    caller reports review and never overwrites it.
    """
    if not output_path.is_file():
        return ExistingOutput.NONE, None
    record = read_record(output_path)
    if record is None or not record.names(
        root_id=root_id, relative_path=relative_path, clip=output_path.name
    ):
        return ExistingOutput.COLLISION, record
    if source_fingerprint is not None and not fingerprint_confirms(
        record.source, source_fingerprint
    ):
        return ExistingOutput.COLLISION, record
    try:
        current = fingerprint(output_path)
    except OSError:
        return ExistingOutput.COLLISION, record
    if not fingerprint_confirms(record.output, current):
        return ExistingOutput.COLLISION, record
    if validate is not None:
        try:
            if not bool(validate(output_path)):
                return ExistingOutput.COLLISION, record
        except Exception:  # a failing probe must never authorise a skip
            return ExistingOutput.COLLISION, record
    return ExistingOutput.SKIP, record


class ExistingOutput(StrEnum):
    """What an existing destination file means for the job."""

    NONE = "none"
    SKIP = "skip"
    COLLISION = "collision"


def _fingerprint_to_json(fingerprint: Fingerprint) -> dict[str, object]:
    return {
        "size_bytes": fingerprint.size_bytes,
        "mtime_ns": fingerprint.mtime_ns,
        "digest": fingerprint.digest,
    }


def _fingerprint_from_json(value: object) -> Fingerprint | None:
    if not isinstance(value, dict):
        return None
    try:
        return Fingerprint(
            size_bytes=int(value["size_bytes"]),
            mtime_ns=int(value["mtime_ns"]),
            digest=str(value["digest"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
