"""Per-folder completion markers (``manifest.json``) for processed footage.

A marker records which clips in its folder were replaced, with the output
fingerprint, so reruns can skip valid completed clips even when the central
manifest is lost or the template later changes. The replacement pipeline
(issues #5/#6) writes markers; scans only read them.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from videocompressor.core.models import Fingerprint

#: Marker file name written into each processed footage folder.
MARKER_FILENAME = "manifest.json"

#: Marker schema version this code reads and writes.
MARKER_VERSION = 1


@dataclass(frozen=True)
class MarkerClip:
    """One completed clip recorded in a folder marker."""

    output: Fingerprint
    completed_at: datetime | None = None


@dataclass(frozen=True)
class FolderMarker:
    """Parsed contents of a folder marker file."""

    template_id: str | None = None
    settings_hash: str | None = None
    generated_at: datetime | None = None
    clips: Mapping[str, MarkerClip] = field(default_factory=dict)
    version: int = MARKER_VERSION


def marker_path(folder: Path) -> Path:
    """Return the marker file path for ``folder``."""
    return folder / MARKER_FILENAME


def read_marker(folder: Path) -> FolderMarker | None:
    """Read the marker in ``folder``.

    A missing, unreadable, corrupt, or unknown-version marker reads as ``None``
    so a rerun degrades to manifest-only classification instead of failing.
    """
    try:
        with marker_path(folder).open("rb") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != MARKER_VERSION:
        return None
    clips: dict[str, MarkerClip] = {}
    raw_clips = data.get("clips")
    if isinstance(raw_clips, dict):
        for name, entry in raw_clips.items():
            clip = _clip_from_json(entry)
            if clip is not None:
                clips[str(name)] = clip
    return FolderMarker(
        template_id=_optional_str(data.get("template_id")),
        settings_hash=_optional_str(data.get("settings_hash")),
        generated_at=_parse_datetime(data.get("generated_at")),
        clips=clips,
    )


def write_marker(folder: Path, marker: FolderMarker) -> Path:
    """Atomically write ``marker`` into ``folder`` and return the marker path."""
    path = marker_path(folder)
    payload: dict[str, object] = {
        "version": MARKER_VERSION,
        "generated_at": (marker.generated_at or datetime.now(UTC)).isoformat(),
        "template_id": marker.template_id,
        "settings_hash": marker.settings_hash,
        "clips": {
            name: {
                "completed_at": clip.completed_at.isoformat() if clip.completed_at else None,
                "output": {
                    "size_bytes": clip.output.size_bytes,
                    "mtime_ns": clip.output.mtime_ns,
                    "digest": clip.output.digest,
                },
            }
            for name, clip in sorted(marker.clips.items())
        },
    }
    file_descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{MARKER_FILENAME}.", suffix=".tmp", dir=folder
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


def mark_completed(
    folder: Path,
    *,
    filename: str,
    output: Fingerprint,
    template_id: str | None = None,
    settings_hash: str | None = None,
    completed_at: datetime | None = None,
    existing: FolderMarker | None = None,
) -> FolderMarker:
    """Record one completed clip in the folder's marker and write it.

    Called by the replacement pipeline after a validated replacement; keeps
    earlier completions in the marker and refreshes the template identity.
    """
    base = existing if existing is not None else read_marker(folder)
    clips = dict(base.clips) if base is not None else {}
    clips[filename] = MarkerClip(
        output=output,
        completed_at=completed_at if completed_at is not None else datetime.now(UTC),
    )
    marker = FolderMarker(
        template_id=template_id,
        settings_hash=settings_hash,
        generated_at=datetime.now(UTC),
        clips=clips,
    )
    write_marker(folder, marker)
    return marker


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _clip_from_json(entry: object) -> MarkerClip | None:
    if not isinstance(entry, dict):
        return None
    output = entry.get("output")
    if not isinstance(output, dict):
        return None
    try:
        fingerprint = Fingerprint(
            size_bytes=int(output["size_bytes"]),
            mtime_ns=int(output["mtime_ns"]),
            digest=str(output["digest"]),
        )
    except (KeyError, TypeError, ValueError):
        return None
    return MarkerClip(output=fingerprint, completed_at=_parse_datetime(entry.get("completed_at")))
