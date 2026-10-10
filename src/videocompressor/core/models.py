"""Shared domain models for Video Compressor.

These types are frozen in issue #1 so later issues can build against them before
their implementations land. Add fields additively; update ``docs/architecture.md``
when a shape changes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, time
from enum import StrEnum
from pathlib import Path


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
    """Day/evening bitrate bucket for one clip (issue #2)."""

    DAYTIME = "daytime"
    EVENING = "evening"


class OriginalHandling(StrEnum):
    """What happens to the source file after a validated replacement."""

    REPLACE_AFTER_VALIDATED_TRANSFER = "replace_after_validated_transfer"


@dataclass(frozen=True)
class TemplateSettings:
    """Encoding template; defaults are the motorcycle template from plan.md."""

    output_height: int = 720
    daytime_video_bitrate_kbps: int = 800
    evening_video_bitrate_kbps: int = 800
    evening_start: time = time(17, 30)
    timezone: str = "Asia/Manila"
    audio_codec: str = "aac"
    audio_bitrate_kbps: int = 96
    ffmpeg_preset: str = "medium"
    original_handling: OriginalHandling = OriginalHandling.REPLACE_AFTER_VALIDATED_TRANSFER


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
    """One row of the central manifest (created in #3, updated in #5-#7)."""

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
