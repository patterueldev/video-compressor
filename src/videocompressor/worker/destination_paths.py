"""Destination folder and output-name resolution for claimed jobs (issues #40, #87).

The pipeline hands a claimed job's raw destination spec to
:func:`destination_from_job` and its optional naming spec to
:func:`naming_from_job`; :func:`destination_folder` resolves the folder on this
node. Destination specs name the output **folder** only:

- ``output_root`` plus a non-empty ``relative_path`` is a folder under the
  registered root (``root/<relative_path>``). An omitted or empty
  ``relative_path`` is the mirror form: the source's own archive-relative
  folders are kept under the destination root (``root/<source dirs>``, the root
  itself when the source sits at the root). Intermediate folders are created.
- ``custom_folder`` is a node-local absolute folder that must already exist.

The output file name is the source clip's own name, or — when the claim carries
a non-default naming spec — the rendered :mod:`videocompressor.core.naming`
pattern plus the source's suffix (:func:`output_name`).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from videocompressor.core.capture_time import interpret_timestamp
from videocompressor.core.models import CaptureTimeMode
from videocompressor.core.naming import (
    DEFAULT_PATTERN,
    render_output_name,
    validate_pattern,
)
from videocompressor.core.validate import MediaFacts
from videocompressor.worker.client import ClaimedJob
from videocompressor.worker.config import MappingError, WorkerConfig

__all__ = [
    "Destination",
    "DestinationKind",
    "DestinationSpecError",
    "NamingSpec",
    "destination_folder",
    "destination_from_job",
    "naming_from_job",
    "naming_time_from_claim",
    "naming_time_from_facts",
    "output_name",
]


class DestinationKind(StrEnum):
    """The three destination forms of the convert flow (issue #39 wording)."""

    SAME_FOLDER = "same_folder"
    OUTPUT_ROOT = "output_root"
    CUSTOM_FOLDER = "custom_folder"


class DestinationSpecError(ValueError):
    """A claimed job's destination spec cannot be used on this node."""


@dataclass(frozen=True)
class Destination:
    """A decoded destination spec (``None`` destination means same-folder/legacy)."""

    kind: DestinationKind
    root_id: str | None = None
    relative_path: str | None = None
    folder: Path | None = None


@dataclass(frozen=True)
class NamingSpec:
    """The claim's output-name pattern resolved for this node (issue #87).

    ``timezone`` and ``capture_mode`` are the controller template's values; the
    worker uses them only to parse the source's raw ``creation_time`` tag when
    the claim carried no resolved capture time.
    """

    pattern: str
    timezone: str
    capture_mode: CaptureTimeMode = CaptureTimeMode.LOCAL_WALL_CLOCK


def destination_from_job(job: ClaimedJob) -> Destination | None:
    """Decode the claim's destination field.

    A missing field (legacy claim) reads as ``None``. Malformed shapes raise
    :class:`DestinationSpecError`, which the caller reports as a ``failed`` job.
    An explicit ``relative_path`` is a root-relative *folder*; an omitted or
    empty one is the mirror form.
    """
    raw = job.destination
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise DestinationSpecError("'destination' must be an object")
    kind = raw.get("kind")
    if kind == DestinationKind.SAME_FOLDER:
        return Destination(kind=DestinationKind.SAME_FOLDER)
    if kind == DestinationKind.OUTPUT_ROOT:
        root_id = raw.get("root_id")
        if not isinstance(root_id, str) or not root_id:
            raise DestinationSpecError("output_root destinations need a root_id")
        relative = raw.get("relative_path")
        if relative is None:
            relative = ""
        if not isinstance(relative, str):
            raise DestinationSpecError("output_root relative_path must be a string")
        return Destination(
            kind=DestinationKind.OUTPUT_ROOT, root_id=root_id, relative_path=relative
        )
    if kind == DestinationKind.CUSTOM_FOLDER:
        folder = raw.get("path")
        if not isinstance(folder, str) or not folder:
            raise DestinationSpecError("custom_folder destinations need a path")
        path = Path(folder).expanduser()
        if not path.is_absolute():
            raise DestinationSpecError(f"custom_folder path must be absolute: {folder!r}")
        return Destination(kind=DestinationKind.CUSTOM_FOLDER, folder=path)
    raise DestinationSpecError(f"unknown destination kind: {kind!r}")


def naming_from_job(job: ClaimedJob) -> NamingSpec | None:
    """Decode the claim's optional naming spec.

    ``None`` when the claim carried nothing usable: a legacy claim has no field,
    a default pattern is not emitted, and a malformed or unknown payload
    degrades to default names instead of failing the job.
    """
    raw = job.naming
    if not isinstance(raw, Mapping):
        return None
    pattern = raw.get("pattern")
    if not isinstance(pattern, str) or not pattern or pattern == DEFAULT_PATTERN:
        return None
    if validate_pattern(pattern):
        return None
    timezone = raw.get("timezone")
    if not isinstance(timezone, str) or not timezone or not _known_zone(timezone):
        return None
    mode = raw.get("capture_mode")
    try:
        capture_mode = (
            CaptureTimeMode(mode) if isinstance(mode, str) else CaptureTimeMode.LOCAL_WALL_CLOCK
        )
    except ValueError:
        capture_mode = CaptureTimeMode.LOCAL_WALL_CLOCK
    return NamingSpec(pattern=pattern, timezone=timezone, capture_mode=capture_mode)


def destination_folder(destination: Destination, config: WorkerConfig, job: ClaimedJob) -> Path:
    """Resolve this node's destination folder, creating root-based folders.

    A ``custom_folder`` must already exist and is never created silently; an
    ``output_root`` resolves through the worker's own mapping (relative, inside
    the root; intermediate folders are created). Raises
    :class:`DestinationSpecError` (or :class:`MappingError` for an unmapped
    root) when the folder cannot be used.
    """
    if destination.kind is DestinationKind.OUTPUT_ROOT:
        if destination.root_id is None:
            raise DestinationSpecError("output_root destination is incomplete")
        relative = destination.relative_path or _source_folder(job.relative_path)
        folder = _root_folder(config, destination.root_id, relative)
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise DestinationSpecError(
                f"cannot create destination folder {folder}: {exc}"
            ) from exc
        return folder
    if destination.kind is DestinationKind.CUSTOM_FOLDER:
        folder = destination.folder
        if folder is None:
            raise DestinationSpecError("custom_folder destination is incomplete")
        if not folder.is_dir():
            raise DestinationSpecError(
                f"destination folder does not exist on this node: {folder}"
            )
        return folder
    raise DestinationSpecError(f"{destination.kind} has no destination folder")


def output_name(naming: NamingSpec | None, source: Path, when: datetime | None) -> str:
    """The output file name for ``source``.

    Without a naming spec (default pattern or legacy claim) the source name is
    kept, so existing flows stay byte-identical. With one, the rendered pattern
    and the source's suffix form the name; :class:`NamePatternError` propagates
    to the caller, which reports a visible per-clip failure.
    """
    if naming is None:
        return source.name
    return render_output_name(naming.pattern, name=source.stem, when=when) + source.suffix


def naming_time_from_claim(naming: NamingSpec, job: ClaimedJob) -> datetime | None:
    """The claim's resolved capture time as wall clock in the naming timezone."""
    raw = job.capture_time
    if not raw:
        return None
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if when.tzinfo is None:
        return when
    return when.astimezone(ZoneInfo(naming.timezone))


def naming_time_from_facts(naming: NamingSpec, facts: MediaFacts) -> datetime | None:
    """The source's raw ``creation_time`` tag parsed under the naming settings."""
    raw = facts.creation_time
    if not raw or not raw.strip():
        return None
    try:
        return interpret_timestamp(raw, mode=naming.capture_mode, timezone=naming.timezone)
    except ValueError:
        return None


# -- internals -------------------------------------------------------------


def _source_folder(relative_path: str) -> str:
    """The archive-relative folder holding ``relative_path`` ('' at a root)."""
    parent = PurePosixPath(relative_path).parent
    return "" if str(parent) == "." else parent.as_posix()


def _root_folder(config: WorkerConfig, root_id: str, relative: str) -> Path:
    """Resolve ``root_id`` plus a root-relative folder; '' means the root itself."""
    if not relative:
        root = config.roots.get(root_id)
        if root is None:
            raise MappingError(f"root_id {root_id!r} is not mapped on this node")
        return root
    return config.resolve(root_id, relative)


def _known_zone(timezone: str) -> bool:
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True
