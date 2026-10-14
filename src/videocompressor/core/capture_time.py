"""Capture-time extraction, timezone interpretation, and classification.

Implements plan.md "Capture time and classification" (issue #2):

- the capture time always comes from ``creation_time`` metadata, never from the
  filesystem;
- format-level tags win over stream-level tags, and candidates that resolve to
  different instants are a review outcome;
- timestamps are interpreted in the configured timezone and classified as
  daytime or evening for the dry-run report.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from videocompressor.core.models import (
    Bucket,
    CaptureIssue,
    CaptureTime,
    CaptureTimeMode,
    MediaInfo,
)

CREATION_TIME_TAG = "creation_time"

#: Reporting boundary for the day/evening label; templates no longer configure it.
DEFAULT_EVENING_START: time = time(17, 30)


def resolve_capture_time(
    media: MediaInfo, *, mode: CaptureTimeMode, timezone: str
) -> CaptureTime:
    """Resolve the capture time of ``media`` under ``mode`` and ``timezone``.

    Candidates are collected from the format-level ``creation_time`` tag first,
    then from every stream in order. Two parseable candidates that resolve to
    different instants produce ``CONFLICTING_TIMESTAMPS``; no parseable candidate
    produces ``MISSING_TIMESTAMP``. Unparseable values are treated as missing and
    the raw value is kept for diagnosis.
    """
    _zone = _load_zone(timezone)  # an unknown timezone is an error even without candidates
    candidates = _creation_time_candidates(media)
    if not candidates:
        return CaptureTime(when=None, mode=mode, issue=CaptureIssue.MISSING_TIMESTAMP)

    first_raw, first_source = candidates[0]
    resolved: list[tuple[datetime, str, str]] = []
    for raw, source in candidates:
        when = interpret_timestamp(raw, mode=mode, timezone=timezone)
        if when is not None:
            resolved.append((when, raw, source))

    if not resolved:
        return CaptureTime(
            when=None,
            raw=first_raw,
            source=first_source,
            mode=mode,
            issue=CaptureIssue.MISSING_TIMESTAMP,
        )
    if len({when for when, _, _ in resolved}) > 1:
        return CaptureTime(
            when=None,
            raw=first_raw,
            source=first_source,
            mode=mode,
            issue=CaptureIssue.CONFLICTING_TIMESTAMPS,
        )

    when, raw, source = resolved[0]
    return CaptureTime(when=when, raw=raw, source=source, mode=mode)


def interpret_timestamp(
    raw: str, *, mode: CaptureTimeMode, timezone: str
) -> datetime | None:
    """Parse one raw ``creation_time`` value, or return ``None`` when it is invalid.

    The public parsing rule :func:`resolve_capture_time` applies to every
    candidate: a trailing ``Z``/``z`` reads as UTC, a value without an offset is
    local wall-clock time in ``timezone``, and a UTC-offset value under
    ``local_wall_clock`` keeps its face value in ``timezone`` (GoPro writes
    local wall-clock time with a trailing ``Z``). Raises ``ValueError`` for an
    unknown timezone name.
    """
    return _parse_timestamp(raw, mode=mode, zone=_load_zone(timezone))


def bucket_for(
    when: datetime, *, timezone: str, evening_start: time = DEFAULT_EVENING_START
) -> Bucket:
    """Classify ``when`` as daytime or evening in ``timezone``.

    Evening starts at ``evening_start`` (default 17:30): exactly 17:30:00 is
    evening, 17:29:59.999999 is daytime. The label feeds the dry-run report and
    the GUI tables only — the single template bitrate applies to both buckets.
    A naive ``when`` is read as local wall-clock time in ``timezone``.
    """
    zone = _load_zone(timezone)
    if when.tzinfo is None:
        when = when.replace(tzinfo=zone)
    local_time = when.astimezone(zone).time().replace(tzinfo=None)
    return Bucket.EVENING if local_time >= evening_start else Bucket.DAYTIME


def _creation_time_candidates(media: MediaInfo) -> list[tuple[str, str]]:
    """Return ``(raw, source)`` for every ``creation_time`` tag, format tag first."""
    candidates: list[tuple[str, str]] = []
    format_raw = media.format_tags.get(CREATION_TIME_TAG)
    if format_raw and format_raw.strip():
        candidates.append((format_raw, "format"))
    for stream in media.streams:
        stream_raw = stream.tags.get(CREATION_TIME_TAG)
        if stream_raw and stream_raw.strip():
            candidates.append((stream_raw, f"stream:{stream.index}"))
    return candidates


def _parse_timestamp(raw: str, *, mode: CaptureTimeMode, zone: ZoneInfo) -> datetime | None:
    """Parse one ``creation_time`` value, or return ``None`` when it is invalid."""
    text = raw.strip()
    if not text:
        return None
    if text[-1] in ("Z", "z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        # Naive timestamps are local wall-clock time in the configured timezone.
        return parsed.replace(tzinfo=zone)
    if mode is CaptureTimeMode.LOCAL_WALL_CLOCK and parsed.utcoffset() == timedelta(0):
        # GoPro writes local wall-clock time with a trailing Z (issue #2 context):
        # keep the face value and read it in the configured timezone.
        return parsed.replace(tzinfo=zone)
    return parsed.astimezone(zone)


def _load_zone(timezone: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown timezone: {timezone!r}") from exc
