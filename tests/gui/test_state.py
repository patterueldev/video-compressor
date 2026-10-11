"""Report state: bitrate/bucket/size derivation and per-clip edits (issue #8)."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

import pytest

from gui.support import DEFAULT_ESTIMATE, EVENING_WHEN, make_report, make_review_row, make_scan_row
from videocompressor.core.models import Bucket, Classification, TemplateSettings
from videocompressor.gui.state import ClipEdit, ReportState, format_bytes


def _state(rows=None, settings=None):
    return ReportState(make_report(rows), settings if settings is not None else TemplateSettings())


def test_rows_derive_from_scan_data():
    complete = make_scan_row(
        "2026-06-Ride/OLD.MP4",
        classification=Classification.COMPLETE,
        capture_time=None,
        bucket=None,
        video_bitrate_kbps=None,
        audio_bitrate_kbps=None,
        estimated_bytes=None,
    )
    state = _state([make_scan_row(), complete])
    rows = state.rows()

    assert [row.relative_path for row in rows] == [
        "2026-06-Ride/GS010001.MP4",
        "2026-06-Ride/OLD.MP4",
    ]
    eligible = rows[0]
    assert eligible.capture_time == EVENING_WHEN
    assert eligible.bucket is Bucket.EVENING
    assert eligible.video_bitrate_kbps == 800
    assert eligible.estimated_bytes == DEFAULT_ESTIMATE
    assert eligible.editable_bitrate and not eligible.editable_capture
    assert not eligible.bitrate_overridden

    skipped = rows[1]
    assert skipped.bucket is None and skipped.video_bitrate_kbps is None
    assert not skipped.editable_bitrate and not skipped.editable_capture


def test_bitrate_override_recomputes_estimate():
    state = _state([make_scan_row()])
    state.set_bitrate("2026-06-Ride/GS010001.MP4", 600)
    row = state.rows()[0]
    assert row.video_bitrate_kbps == 600
    assert row.bitrate_overridden
    assert row.estimated_bytes == 78_300_000  # 900 s at 600 + 96 kbps

    state.set_bitrate("2026-06-Ride/GS010001.MP4", None)
    assert state.rows()[0].estimated_bytes == DEFAULT_ESTIMATE
    assert not state.rows()[0].bitrate_overridden
    assert state.edit("2026-06-Ride/GS010001.MP4") == ClipEdit()


def test_template_settings_drive_effective_bitrate():
    settings = replace(TemplateSettings(), video_bitrate_kbps=1500)
    state = _state([make_scan_row()], settings)
    assert state.rows()[0].video_bitrate_kbps == 1500


def test_manual_capture_time_on_review_row_sets_bucket_and_bitrate():
    state = _state([make_review_row()])
    before = state.rows()[0]
    assert before.bucket is None and not before.editable_bitrate
    assert before.editable_capture

    state.set_capture_time("2026-06-Ride/GX010002.MP4", datetime(2026, 6, 14, 9, 5))
    row = state.rows()[0]
    assert row.capture_manual
    assert row.capture_time == datetime(2026, 6, 14, 9, 5)
    assert row.bucket is Bucket.DAYTIME
    assert row.video_bitrate_kbps == 800
    assert row.editable_bitrate
    assert row.estimated_bytes is None  # scan rows for review clips carry no estimate

    state.set_capture_time("2026-06-Ride/GX010002.MP4", datetime(2026, 6, 14, 18, 0))
    assert state.rows()[0].bucket is Bucket.EVENING

    state.set_capture_time("2026-06-Ride/GX010002.MP4", None)
    assert state.rows()[0].bucket is None
    assert not state.rows()[0].capture_manual


def test_manual_capture_time_uses_the_template_bitrate():
    settings = replace(TemplateSettings(), video_bitrate_kbps=1500)
    state = _state([make_review_row()], settings)
    path = "2026-06-Ride/GX010002.MP4"

    state.set_capture_time(path, datetime(2026, 6, 14, 9, 5))
    assert state.rows()[0].bucket is Bucket.DAYTIME
    assert state.rows()[0].video_bitrate_kbps == 1500
    state.set_capture_time(path, datetime(2026, 6, 14, 17, 30))
    assert state.rows()[0].bucket is Bucket.EVENING  # exactly 17:30 is evening
    assert state.rows()[0].video_bitrate_kbps == 1500  # one bitrate for both buckets


def test_bitrate_override_after_manual_capture_time():
    state = _state([make_review_row()])
    path = "2026-06-Ride/GX010002.MP4"
    state.set_capture_time(path, datetime(2026, 6, 14, 9, 5))
    state.set_bitrate(path, 400)
    row = state.rows()[0]
    assert row.video_bitrate_kbps == 400 and row.bitrate_overridden


def test_edit_validation_and_unknown_clips():
    state = _state([make_scan_row()])
    with pytest.raises(ValueError):
        state.set_bitrate("2026-06-Ride/GS010001.MP4", 0)
    with pytest.raises(KeyError):
        state.set_bitrate("missing.mp4", 800)
    with pytest.raises(KeyError):
        state.set_capture_time("missing.mp4", None)


def test_counts_and_estimated_total_react_to_edits():
    rows = [make_scan_row(), make_scan_row("2026-06-Ride/GS010002.MP4"), make_review_row()]
    state = _state(rows)
    assert state.counts() == {
        Classification.NEW: 2,
        Classification.COMPLETE: 0,
        Classification.CHANGED: 0,
        Classification.REVIEW: 1,
    }
    assert state.estimated_bytes() == 2 * DEFAULT_ESTIMATE
    assert len(state.queue_rows()) == 2
    assert len(state.review_rows()) == 1

    state.set_bitrate("2026-06-Ride/GS010001.MP4", 600)
    assert state.estimated_bytes() == 78_300_000 + DEFAULT_ESTIMATE


def test_settings_changed_flag():
    state = _state([make_scan_row()])
    assert not state.settings_changed

    state.set_settings(replace(TemplateSettings(), video_bitrate_kbps=1500))
    assert state.settings_changed
    assert state.rows()[0].video_bitrate_kbps == 1500


def test_format_bytes():
    assert format_bytes(None) == "-"
    assert format_bytes(90_000) == "88 KB"
    assert format_bytes(DEFAULT_ESTIMATE) == "100.8 MB"
