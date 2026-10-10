#!/usr/bin/env python3
"""Shared selection model and sanitization helpers for the issue #12 pilot tools.

``docs/pilot/pilot-selection.toml`` describes the pilot clips:

    [[clips]]
    relative_path = "ARCHIVE/RELATIVE/PATH.MP4"   # archive-relative POSIX path
    category = "daytime" | "evening" | "dim" | "motion-heavy"
    bucket = "daytime" | "evening"
    candidates = [600, 800, 1000, 1200]           # sweep video bitrates (kbps)
    segment = { start_s = 0.0, duration_s = 90.0 }   # optional trim
    shots = [{ t_s = 12.5, crop = { w = 480, h = 270, x = 800, y = 300 }, note = "sign" }]

Shot crops are pixel windows in the 720p reference frame; the tools scale any
input to 720p height before cropping. Everything these tools write into the
repository is sanitized: archive-relative paths only, absolute prefixes
replaced with placeholders by :func:`sanitize_text`.

Loaded as a sibling module by ``scripts/pilot_run.py`` and
``scripts/bitrate_sweep.py`` (run them with the project virtualenv).
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

__all__ = [
    "ARCHIVE_PLACEHOLDER",
    "BUCKETS",
    "CATEGORIES",
    "PILOT_PLACEHOLDER",
    "SCRATCH_PLACEHOLDER",
    "SelectionClip",
    "SelectionError",
    "Segment",
    "Shot",
    "ToolError",
    "assert_outside_root",
    "is_safe_relative_path",
    "load_selection",
    "resolve_under_root",
    "sanitize_text",
    "scaled_width_for_height",
    "shot_label",
    "slug",
    "validate_crop",
]

CATEGORIES = ("daytime", "evening", "dim", "motion-heavy")
BUCKETS = ("daytime", "evening")

ARCHIVE_PLACEHOLDER = "<archive-root>"
PILOT_PLACEHOLDER = "<pilot-root>"
SCRATCH_PLACEHOLDER = "<scratch>"


class SelectionError(ValueError):
    """The selection file or a tool path is invalid (usage error, exit 2)."""


class ToolError(RuntimeError):
    """An operational failure (exit 1): missing clip, mismatch, failed encode."""


@dataclass(frozen=True)
class Segment:
    """A trim of one clip for the sweep: start offset and duration in seconds."""

    start_s: float
    duration_s: float


@dataclass(frozen=True)
class Shot:
    """One sign/plate comparison shot: timestamp plus a crop at 720p scale."""

    t_s: float
    crop_w: int
    crop_h: int
    crop_x: int
    crop_y: int
    note: str = ""


@dataclass(frozen=True)
class SelectionClip:
    """One pilot clip and how the tools should treat it."""

    relative_path: str
    category: str
    bucket: str
    candidates: tuple[int, ...]
    segment: Segment | None = None
    shots: tuple[Shot, ...] = ()


def load_selection(path: Path) -> tuple[SelectionClip, ...]:
    """Parse and validate a pilot selection file."""
    if not path.is_file():
        raise SelectionError(f"selection file {path} does not exist")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SelectionError(f"cannot read selection {path}: {exc}") from exc
    raw_clips = data.get("clips")
    if not isinstance(raw_clips, list) or not raw_clips:
        raise SelectionError(f"selection {path} needs a non-empty [[clips]] list")
    return tuple(_clip(index, raw, path) for index, raw in enumerate(raw_clips))


def is_safe_relative_path(value: str) -> bool:
    """Whether ``value`` is a safe archive-relative POSIX path."""
    if not value or "\\" in value:
        return False
    pure = PurePosixPath(value)
    return not pure.is_absolute() and all(part not in ("", ".", "..") for part in pure.parts)


def resolve_under_root(root: Path, relative_path: str) -> Path:
    """Join ``relative_path`` under ``root``, refusing escapes (worker-config rules)."""
    if not is_safe_relative_path(relative_path):
        raise SelectionError(f"unsafe archive-relative path {relative_path!r}")
    candidate = root.joinpath(*PurePosixPath(relative_path).parts)
    root_resolved = root.resolve()
    resolved = candidate.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise SelectionError(f"path {relative_path!r} escapes root {root}")
    return candidate


def assert_outside_root(path: Path, root: Path, label: str) -> None:
    """Refuse a tool output path that lives inside the archive root."""
    resolved = path.expanduser().resolve()
    root_resolved = root.resolve()
    if resolved == root_resolved or root_resolved in resolved.parents:
        raise SelectionError(f"{label} {path} must live outside the archive root {root}")


def sanitize_text(text: str, replacements: Mapping[str, str]) -> str:
    """Replace absolute path prefixes with placeholders; longest prefix first."""
    for needle in sorted(replacements, key=len, reverse=True):
        if needle:
            text = text.replace(needle, replacements[needle])
    return text


def scaled_width_for_height(
    source_width: int, source_height: int, *, output_height: int = 720
) -> int:
    """Width of ``scale=-2:output_height`` output, floored to an even number."""
    if source_width < 2 or source_height < 2 or output_height < 2:
        raise SelectionError(
            f"invalid dimensions {source_width}x{source_height} -> {output_height}p"
        )
    width = int(output_height * source_width / source_height)
    return max(2, width // 2 * 2)


def validate_crop(shot: Shot, scaled_width: int, scaled_height: int) -> None:
    """Refuse a shot crop that does not fit the scaled reference frame."""
    if shot.crop_w < 2 or shot.crop_h < 2:
        raise SelectionError("shot crop must be at least 2x2 pixels")
    if (
        shot.crop_x < 0
        or shot.crop_y < 0
        or shot.crop_x + shot.crop_w > scaled_width
        or shot.crop_y + shot.crop_h > scaled_height
    ):
        raise SelectionError(
            f"shot crop {shot.crop_w}x{shot.crop_h}+{shot.crop_x}+{shot.crop_y} "
            f"does not fit the {scaled_width}x{scaled_height} reference frame"
        )


def slug(relative_path: str) -> str:
    """Stable, path-free slug for one clip (``VIDEOS/MOTORCYCLE/A.MP4`` -> ``...``)."""
    stem = PurePosixPath(relative_path).with_suffix("").as_posix()
    return re.sub(r"[^A-Za-z0-9._-]+", "_", stem.replace("/", "__"))


def shot_label(shot: Shot, index: int) -> str:
    """Short file-name label for one shot: its note slug, or ``shot<n>``."""
    label = re.sub(r"[^a-z0-9]+", "-", shot.note.lower()).strip("-")
    return label or f"shot{index}"


# -- parsing helpers ----------------------------------------------------------


def _clip(index: int, raw: object, path: Path) -> SelectionClip:
    where = f"{path}: clips[{index}]"
    if not isinstance(raw, Mapping):
        raise SelectionError(f"{where} must be a table")
    relative_path = _text(raw, "relative_path", where)
    if not is_safe_relative_path(relative_path):
        raise SelectionError(
            f"{where}: relative_path {relative_path!r} must be a safe archive-relative POSIX path"
        )
    category = _text(raw, "category", where)
    if category not in CATEGORIES:
        raise SelectionError(f"{where}: category must be one of {', '.join(CATEGORIES)}")
    bucket = _text(raw, "bucket", where)
    if bucket not in BUCKETS:
        raise SelectionError(f"{where}: bucket must be one of {', '.join(BUCKETS)}")
    candidates = raw.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise SelectionError(f"{where}: candidates must be a non-empty list of kbps integers")
    for candidate in candidates:
        if isinstance(candidate, bool) or not isinstance(candidate, int) or candidate <= 0:
            raise SelectionError(f"{where}: candidates must be positive kbps integers")
    segment = _segment(raw.get("segment"), where)
    shots = _shots(raw.get("shots"), where)
    return SelectionClip(
        relative_path=relative_path,
        category=category,
        bucket=bucket,
        candidates=tuple(candidates),
        segment=segment,
        shots=shots,
    )


def _segment(raw: object, where: str) -> Segment | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise SelectionError(f"{where}: segment must be a table with start_s/duration_s")
    start_s = _number(raw.get("start_s"), where, "segment.start_s")
    duration_s = _number(raw.get("duration_s"), where, "segment.duration_s")
    if start_s < 0 or duration_s <= 0:
        raise SelectionError(f"{where}: segment needs start_s >= 0 and duration_s > 0")
    return Segment(start_s=start_s, duration_s=duration_s)


def _shots(raw: object, where: str) -> tuple[Shot, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise SelectionError(f"{where}: shots must be a list of tables")
    return tuple(_shot(index, entry, where) for index, entry in enumerate(raw))


def _shot(index: int, raw: object, where: str) -> Shot:
    what = f"{where}: shots[{index}]"
    if not isinstance(raw, Mapping):
        raise SelectionError(f"{what} must be a table")
    crop = raw.get("crop")
    if not isinstance(crop, Mapping):
        raise SelectionError(f"{what}: crop must be a table with w/h/x/y pixels at 720p scale")
    return Shot(
        t_s=_number(raw.get("t_s"), where, f"{what}.t_s"),
        crop_w=_int(crop.get("w"), where, f"{what}.crop.w"),
        crop_h=_int(crop.get("h"), where, f"{what}.crop.h"),
        crop_x=_int(crop.get("x"), where, f"{what}.crop.x"),
        crop_y=_int(crop.get("y"), where, f"{what}.crop.y"),
        note=str(raw.get("note", "")),
    )


def _text(raw: Mapping[str, Any], key: str, where: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise SelectionError(f"{where}: {key} must be a non-empty string")
    return value


def _number(value: object, where: str, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SelectionError(f"{where}: {label} must be a number")
    return float(value)


def _int(value: object, where: str, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SelectionError(f"{where}: {label} must be an integer")
    return value
