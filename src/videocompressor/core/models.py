"""Shared domain models for Video Compressor.

These types are frozen in issue #1 so later issues can build against them before
their implementations land. Add fields additively; update ``docs/architecture.md``
when a shape changes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath


class JobState(StrEnum):
    """Lifecycle stage of one clip through the queue (plan.md processing states)."""

    QUEUED = "queued"
    CLAIMED = "claimed"
    COPIED = "copied"
    ENCODING = "encoding"
    VALIDATED = "validated"
    TRANSFERRING = "transferring"
    REPLACING = "replacing"
    COMPLETE = "complete"
    FAILED = "failed"
    REVIEW = "review"


class Classification(StrEnum):
    """How a scanned clip relates to the manifest (plan.md workflow step 3)."""

    NEW = "new"
    COMPLETE = "complete"
    CHANGED = "changed"
    REVIEW = "review"


class CaptureIssue(StrEnum):
    """Reasons a clip's capture time cannot be trusted (issue #2)."""

    MISSING_TIMESTAMP = "missing_timestamp"
    CONFLICTING_TIMESTAMPS = "conflicting_timestamps"
    UNREADABLE = "unreadable"
    UNSUPPORTED = "unsupported"


class CaptureTimeMode(StrEnum):
    """How capture timestamps are interpreted (issue #2; config default in #1)."""

    LOCAL_WALL_CLOCK = "local_wall_clock"
    UTC = "utc"


class Bucket(StrEnum):
    """Day/evening classification label for one clip (issue #2).

    The single template bitrate applies to both buckets; the label only feeds
    the dry-run report and the GUI tables.
    """

    DAYTIME = "daytime"
    EVENING = "evening"


class OriginalHandling(StrEnum):
    """What happens to the source file after a validated replacement."""

    REPLACE_AFTER_VALIDATED_TRANSFER = "replace_after_validated_transfer"


class DestinationKind(StrEnum):
    """Where a direct-conversion output is placed (issue #39).

    ``same_folder`` is the verified swap: the output replaces the source at its
    own path. ``output_root`` places the output under a registered root plus a
    root-relative folder.
    """

    SAME_FOLDER = "same_folder"
    OUTPUT_ROOT = "output_root"


def is_safe_relative_path(value: str, *, allow_empty: bool = False) -> bool:
    """Whether ``value`` is a safe relative POSIX path (no escapes or backslashes)."""
    if not value:
        return allow_empty
    if "\\" in value:
        return False
    pure = PurePosixPath(value)
    return not pure.is_absolute() and all(part not in ("", ".", "..") for part in pure.parts)


@dataclass(frozen=True)
class Destination:
    """Destination spec for a direct-conversion job (issue #39).

    Names the output folder only; the output filename and collision handling
    belong to the worker pipeline (issue #40). An ``output_root`` destination
    resolves ``root_id`` + ``relative_path`` per node; an empty ``relative_path``
    means the node mirrors the source's archive-relative path under the root
    (the worker's fallback). ``same_folder`` replaces the source at its own path.
    """

    kind: DestinationKind
    root_id: str | None = None
    relative_path: str = ""

    def __post_init__(self) -> None:
        if self.kind is DestinationKind.SAME_FOLDER:
            if self.root_id is not None or self.relative_path:
                raise ValueError("a same-folder destination takes no root or relative path")
            return
        if not self.root_id:
            raise ValueError("an output-root destination needs a non-empty root_id")
        if not is_safe_relative_path(self.relative_path, allow_empty=True):
            raise ValueError(f"unsafe destination relative path {self.relative_path!r}")


def destination_to_dict(destination: Destination | None) -> dict[str, object] | None:
    """JSON-ready destination spec; ``None`` (legacy jobs) stays ``None``.

    Only meaningful keys are emitted, matching the worker's decoder: an absent
    ``relative_path`` means "mirror the source's archive-relative path".
    """
    if destination is None:
        return None
    payload: dict[str, object] = {"kind": str(destination.kind)}
    if destination.root_id is not None:
        payload["root_id"] = destination.root_id
    if destination.relative_path:
        payload["relative_path"] = destination.relative_path
    return payload


def destination_from_dict(payload: object) -> Destination | None:
    """Parse a destination spec from its JSON form; ``None`` stays ``None``."""
    if payload is None:
        return None
    if not isinstance(payload, Mapping):
        raise ValueError("destination must be an object")
    kind = payload.get("kind")
    if kind not in {member.value for member in DestinationKind}:
        raise ValueError(f"unknown destination kind {kind!r}")
    root_id = payload.get("root_id")
    relative_path = payload.get("relative_path", "")
    if root_id is not None and not isinstance(root_id, str):
        raise ValueError("destination root_id must be a string")
    if not isinstance(relative_path, str):
        raise ValueError("destination relative_path must be a string")
    return Destination(kind=DestinationKind(kind), root_id=root_id, relative_path=relative_path)



@dataclass(frozen=True)
class TemplateSettings:
    """Encoding template; defaults are the motorcycle template from plan.md."""

    output_height: int = 720
    video_bitrate_kbps: int = 800
    timezone: str = "Asia/Manila"
    audio_codec: str = "aac"
    audio_bitrate_kbps: int = 96
    ffmpeg_preset: str = "medium"
    original_handling: OriginalHandling = OriginalHandling.REPLACE_AFTER_VALIDATED_TRANSFER


@dataclass(frozen=True)
class EncodeSettings:
    """Effective encode settings for one job, resolved from a template per clip.

    Matches the ``settings`` object of the controller claim contract
    (docs/architecture.md) and the plan.md motorcycle defaults (issue #4).
    """

    output_height: int = 720
    video_bitrate_kbps: int = 800
    audio_bitrate_kbps: int = 96
    audio_codec: str = "aac"
    ffmpeg_preset: str = "medium"

    def __post_init__(self) -> None:
        if self.output_height < 2 or self.output_height % 2:
            raise ValueError(
                f"output_height must be a positive even number, got {self.output_height}"
            )
        if self.video_bitrate_kbps <= 0:
            raise ValueError(f"video_bitrate_kbps must be positive, got {self.video_bitrate_kbps}")
        if self.audio_bitrate_kbps <= 0:
            raise ValueError(f"audio_bitrate_kbps must be positive, got {self.audio_bitrate_kbps}")


@dataclass(frozen=True)
class ArchiveRoot:
    """A registered archive root as this machine sees it."""

    id: str
    name: str
    path: Path


@dataclass(frozen=True)
class WorkerInfo:
    """Registry entry for a worker node (registered in #6, tracked in #7)."""

    id: str
    name: str
    root_mappings: Mapping[str, Path]
    scratch_dir: Path
    thread_cap: int = 2
    online: bool = False
    last_seen: datetime | None = None


@dataclass(frozen=True)
class Fingerprint:
    """Cheap source fingerprint: size + mtime + sampled digest (issue #3)."""

    size_bytes: int
    mtime_ns: int
    digest: str


@dataclass(frozen=True)
class CaptureTime:
    """Resolved capture time for one clip (issue #2)."""

    when: datetime | None
    raw: str | None = None
    source: str | None = None
    mode: CaptureTimeMode = CaptureTimeMode.LOCAL_WALL_CLOCK
    issue: CaptureIssue | None = None


@dataclass(frozen=True)
class MediaStream:
    """One stream reported by ffprobe (issue #2)."""

    index: int
    codec_type: str
    codec_name: str | None = None
    width: int | None = None
    height: int | None = None
    channels: int | None = None
    codec_tag: str | None = None
    tags: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class MediaInfo:
    """Result of probing one media file (``core.probe.probe_media``, issue #2)."""

    path: Path
    format_name: str | None = None
    duration_s: float | None = None
    size_bytes: int | None = None
    bit_rate: int | None = None
    format_tags: Mapping[str, str] = field(default_factory=dict)
    streams: tuple[MediaStream, ...] = ()


@dataclass(frozen=True)
class ClipRecord:
    """One row of the central manifest (created in #3, updated through #39)."""

    root_id: str
    relative_path: str  # archive-root-relative POSIX path
    source: Fingerprint
    state: JobState = JobState.QUEUED
    classification: Classification = Classification.NEW
    capture_time: CaptureTime | None = None
    template_id: str | None = None
    settings_hash: str | None = None
    worker_id: str | None = None
    output: Fingerprint | None = None
    error: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    completed_at: datetime | None = None
    destination: Destination | None = None  # None = legacy replace-at-source job
    delete_original: bool = False


@dataclass(frozen=True)
class RunEvent:
    """One entry of the manifest run log (backing the controller events endpoint)."""

    seq: int
    at: datetime
    level: str
    message: str
    root_id: str | None = None
    relative_path: str | None = None
