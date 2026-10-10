"""Editable view state behind the queue and review tables (issue #8).

Pure Python — no PySide6 import — so the derivation rules stay testable in CI
without the optional ``gui`` extra. Edits live in this session only: the dry run
never writes, and the durable queue write belongs to the processing slice.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from videocompressor.core.capture_time import bucket_for
from videocompressor.core.models import Bucket, Classification, TemplateSettings
from videocompressor.core.scan import ScanReport, ScanRow, settings_hash


@dataclass(frozen=True)
class ClipEdit:
    """Operator overrides for one clip; empty means "no edits"."""

    bitrate_kbps: int | None = None
    capture_time: datetime | None = None  # naive wall clock in the template timezone


@dataclass(frozen=True)
class DisplayRow:
    """One derived table row: scan row plus any operator edits."""

    relative_path: str
    classification: Classification
    capture_time: datetime | None
    capture_manual: bool
    bucket: Bucket | None
    video_bitrate_kbps: int | None
    bitrate_overridden: bool
    audio_bitrate_kbps: int | None
    estimated_bytes: int | None
    size_bytes: int | None
    note: str | None
    editable_bitrate: bool
    editable_capture: bool


class ReportState:
    """Edit overrides on top of one scan report, with derived bitrate and size.

    The effective bucket and bitrate follow the selected template: a manual capture
    time is classified with :func:`bucket_for`, and the template's day/evening
    bitrate wins unless the operator overrode the bitrate for the clip. Overriding
    a bitrate re-estimates the output size proportionally to the scanned estimate
    (the scan row carries the estimate, not the clip duration).
    """

    def __init__(self, report: ScanReport, settings: TemplateSettings) -> None:
        self.report = report
        self.settings = settings
        self._edits: dict[str, ClipEdit] = {}
        self._rows_by_path = {row.relative_path: row for row in report.rows}

    # -- settings ----------------------------------------------------------

    def set_settings(self, settings: TemplateSettings) -> None:
        """Use ``settings`` for derived bitrates; rows re-derive on the next read."""
        self.settings = settings

    @property
    def settings_changed(self) -> bool:
        """True when the current settings differ from the ones this scan used."""
        return settings_hash(self.settings) != self.report.settings_hash

    # -- edits -------------------------------------------------------------

    def edit(self, relative_path: str) -> ClipEdit:
        """The current overrides for ``relative_path`` (empty when none)."""
        return self._edits.get(relative_path, ClipEdit())

    def set_bitrate(self, relative_path: str, kbps: int | None) -> None:
        """Override (or clear with ``None``) the video bitrate of one clip."""
        if kbps is not None and kbps <= 0:
            raise ValueError("bitrate must be positive")
        self._update(relative_path, bitrate_kbps=kbps)

    def set_capture_time(self, relative_path: str, when: datetime | None) -> None:
        """Set (or clear with ``None``) a manual capture time for one clip."""
        self._update(relative_path, capture_time=when)

    def _update(self, relative_path: str, **changes: object) -> None:
        if relative_path not in self._rows_by_path:
            raise KeyError(f"unknown clip {relative_path!r}")
        edited = replace(self.edit(relative_path), **changes)
        if edited == ClipEdit():
            self._edits.pop(relative_path, None)
        else:
            self._edits[relative_path] = edited

    # -- derived views -----------------------------------------------------

    def rows(self) -> tuple[DisplayRow, ...]:
        """Every scan row with the operator edits applied."""
        return tuple(self._display(row) for row in self.report.rows)

    def queue_rows(self) -> tuple[DisplayRow, ...]:
        """Rows the queue table shows: everything that is not in review."""
        return tuple(row for row in self.rows() if row.classification is not Classification.REVIEW)

    def review_rows(self) -> tuple[DisplayRow, ...]:
        """Rows the review table shows."""
        return tuple(row for row in self.rows() if row.classification is Classification.REVIEW)

    def counts(self) -> dict[Classification, int]:
        """Clip counts per classification, in model order."""
        counts = {kind: 0 for kind in Classification}
        for row in self.report.rows:
            counts[row.classification] += 1
        return counts

    def estimated_bytes(self) -> int:
        """Total estimated output size across the displayed rows."""
        return sum(row.estimated_bytes or 0 for row in self.rows())

    def _display(self, row: ScanRow) -> DisplayRow:
        edit = self.edit(row.relative_path)
        manual = edit.capture_time is not None
        scanned_time = row.capture_time.when if row.capture_time is not None else None
        capture = edit.capture_time if manual else scanned_time
        bucket = (
            bucket_for(
                capture,
                evening_start=self.settings.evening_start,
                timezone=self.settings.timezone,
            )
            if capture is not None
            else row.bucket
        )
        bitrate = self._bitrate(row, edit, bucket)
        return DisplayRow(
            relative_path=row.relative_path,
            classification=row.classification,
            capture_time=capture,
            capture_manual=manual,
            bucket=bucket,
            video_bitrate_kbps=bitrate,
            bitrate_overridden=edit.bitrate_kbps is not None,
            audio_bitrate_kbps=row.audio_bitrate_kbps,
            estimated_bytes=self._estimate(row, edit, bitrate),
            size_bytes=row.size_bytes,
            note=row.note,
            editable_bitrate=row.classification is Classification.NEW or manual,
            editable_capture=row.classification is Classification.REVIEW,
        )

    def _bitrate(self, row: ScanRow, edit: ClipEdit, bucket: Bucket | None) -> int | None:
        if edit.bitrate_kbps is not None:
            return edit.bitrate_kbps
        if bucket is Bucket.EVENING:
            return self.settings.evening_video_bitrate_kbps
        if bucket is Bucket.DAYTIME:
            return self.settings.daytime_video_bitrate_kbps
        return row.video_bitrate_kbps

    @staticmethod
    def _estimate(row: ScanRow, edit: ClipEdit, bitrate: int | None) -> int | None:
        if row.estimated_bytes is None:
            return None
        if edit.bitrate_kbps is None or edit.bitrate_kbps == row.video_bitrate_kbps:
            return row.estimated_bytes
        if row.video_bitrate_kbps is None or bitrate is None:
            return row.estimated_bytes
        base_total = row.video_bitrate_kbps + (row.audio_bitrate_kbps or 0)
        if base_total <= 0:
            return row.estimated_bytes
        new_total = bitrate + (row.audio_bitrate_kbps or 0)
        return round(row.estimated_bytes * new_total / base_total)


def format_bytes(value: int | None) -> str:
    """Human-readable size for the tables (mirrors the CLI dry-run style)."""
    if value is None:
        return "-"
    if value < 1_000_000:
        return f"{value / 1024:.0f} KB"
    return f"{value / 1_000_000:.1f} MB"
