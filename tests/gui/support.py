"""Shared factories for the GUI tests (issue #8).

No Qt imports here on purpose: the pure-Python suites use these helpers and keep
running without the optional ``gui`` extra.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from videocompressor.core.config import DEFAULT_TEMPLATE_NAME
from videocompressor.core.models import (
    Bucket,
    CaptureTime,
    CaptureTimeMode,
    Classification,
    TemplateSettings,
)
from videocompressor.core.scan import ScanReport, ScanRow, estimate_output_bytes, settings_hash

#: Template used by report factories; the plan.md motorcycle defaults.
SETTINGS = TemplateSettings()

#: Evening capture time just after the default 17:30 boundary, +08:00.
EVENING_WHEN = datetime(2026, 6, 14, 17, 41, tzinfo=timezone(timedelta(hours=8)))

#: Daytime capture time well before the default 17:30 boundary, +08:00.
DAYTIME_WHEN = datetime(2026, 6, 14, 9, 5, tzinfo=timezone(timedelta(hours=8)))

#: Estimate for a 15-minute clip at the motorcycle defaults (900 s, 800 + 96 kbps).
DEFAULT_ESTIMATE = estimate_output_bytes(
    900.0, video_bitrate_kbps=800, audio_bitrate_kbps=96
)


def make_scan_row(relative_path: str = "2026-06-Ride/GS010001.MP4", **overrides: object) -> ScanRow:
    """A scan row; defaults describe an eligible evening clip at the template defaults."""
    fields: dict[str, object] = {
        "relative_path": relative_path,
        "classification": Classification.NEW,
        "size_bytes": 4_000_000,
        "mtime_ns": 1_750_000_000_000_000_000,
        "digest": "a" * 16,
        "capture_time": CaptureTime(
            when=EVENING_WHEN,
            raw="2026-06-14 09:41:00Z",
            source="format",
            mode=CaptureTimeMode.LOCAL_WALL_CLOCK,
        ),
        "bucket": Bucket.EVENING,
        "video_bitrate_kbps": 800,
        "audio_bitrate_kbps": 96,
        "estimated_bytes": DEFAULT_ESTIMATE,
        "note": None,
    }
    fields.update(overrides)
    return ScanRow(**fields)  # type: ignore[arg-type]


def make_review_row(
    relative_path: str = "2026-06-Ride/GX010002.MP4", **overrides: object
) -> ScanRow:
    """A review row without a usable capture time (missing timestamp)."""
    fields: dict[str, object] = {
        "capture_time": CaptureTime(when=None, mode=CaptureTimeMode.LOCAL_WALL_CLOCK),
        "bucket": None,
        "video_bitrate_kbps": None,
        "audio_bitrate_kbps": None,
        "estimated_bytes": None,
        "note": "capture time missing",
    }
    fields.update(overrides)
    return make_scan_row(relative_path, classification=Classification.REVIEW, **fields)


def make_report(
    rows: Sequence[ScanRow] | None = None,
    *,
    folder_relative: str = "2026-06-Ride",
    settings: TemplateSettings = SETTINGS,
) -> ScanReport:
    """A ``ScanReport`` over a synthetic archive root."""
    return ScanReport(
        root_id="camera",
        root_path=Path("/archive"),
        folder=Path("/archive") / folder_relative,
        folder_relative=folder_relative,
        template_id=DEFAULT_TEMPLATE_NAME,
        settings_hash=settings_hash(settings),
        generated_at=datetime(2026, 6, 15, 12, 0, tzinfo=UTC),
        rows=tuple(rows) if rows is not None else (),
    )
