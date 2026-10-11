"""Clip tables: queue/review split, edit roles, and the summary (issue #8)."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt

from gui.support import make_report, make_review_row, make_scan_row
from videocompressor.core.models import Bucket, Classification, TemplateSettings
from videocompressor.gui.clip_tables import ClipTablesPanel

_CLIP, _CAPTURE, _BUCKET, _BITRATE, _ESTIMATE, _STATE, _NOTE = range(7)


@pytest.fixture
def panel(qapp):
    return ClipTablesPanel()


def test_tables_split_queue_and_review(panel):
    report = make_report(
        [
            make_scan_row("2026-06-Ride/GS010001.MP4"),
            make_scan_row("2026-06-Ride/OLD.MP4", classification=Classification.COMPLETE),
            make_review_row(),
        ]
    )
    panel.set_report(report, TemplateSettings())

    assert panel.queue_model.rowCount() == 2
    assert panel.review_model.rowCount() == 1
    summary = panel.summary_text()
    assert "3 clips" in summary
    assert "1 new" in summary and "1 complete" in summary and "1 review" in summary
    assert panel.stale_notice() == ""


def test_bitrate_override_updates_state_and_summary(panel):
    panel.set_report(make_report([make_scan_row()]), TemplateSettings())
    model = panel.queue_model
    bitrate_index = model.index(0, _BITRATE)

    assert model.flags(bitrate_index) & Qt.ItemFlag.ItemIsEditable
    assert model.setData(bitrate_index, 600, Qt.ItemDataRole.EditRole)

    row = panel.state().rows()[0]
    assert row.video_bitrate_kbps == 600 and row.bitrate_overridden
    assert "(override)" in model.data(bitrate_index, Qt.ItemDataRole.DisplayRole)
    assert panel.summary_text().count("estimated output") == 1
    assert model.data(model.index(0, _ESTIMATE), Qt.ItemDataRole.DisplayRole) == "78.3 MB"


def test_review_capture_time_edit_updates_bucket(panel):
    panel.set_report(make_report([make_review_row()]), TemplateSettings())
    model = panel.review_model
    capture_index = model.index(0, _CAPTURE)

    assert model.flags(capture_index) & Qt.ItemFlag.ItemIsEditable
    assert model.setData(capture_index, datetime(2026, 6, 14, 9, 5), Qt.ItemDataRole.EditRole)

    row = panel.state().review_rows()[0]
    assert row.capture_manual and row.bucket is Bucket.DAYTIME
    assert row.video_bitrate_kbps == 800
    display = model.data(capture_index, Qt.ItemDataRole.DisplayRole)
    assert "2026-06-14 09:05" in display and "(manual)" in display


def test_non_editable_cells_reject_edits(panel):
    panel.set_report(
        make_report(
            [
                make_scan_row(),
                make_scan_row("2026-06-Ride/OLD.MP4", classification=Classification.COMPLETE),
                make_review_row(),
            ]
        ),
        TemplateSettings(),
    )
    queue = panel.queue_model
    assert not queue.flags(queue.index(0, _CAPTURE)) & Qt.ItemFlag.ItemIsEditable
    assert not queue.setData(queue.index(0, _CLIP), "other")
    assert not queue.setData(queue.index(0, _STATE), "review")

    # complete rows accept no bitrate edit either
    complete_row = next(
        row
        for row in range(queue.rowCount())
        if queue.row_at(row).classification is Classification.COMPLETE
    )
    assert not queue.flags(queue.index(complete_row, _BITRATE)) & Qt.ItemFlag.ItemIsEditable

    # review capture is not editable in the queue view (the review table shows it)
    review = panel.review_model
    assert review.flags(review.index(0, _CAPTURE)) & Qt.ItemFlag.ItemIsEditable


def test_template_change_marks_the_scan_stale(panel):
    panel.set_report(make_report([make_scan_row()]), TemplateSettings())
    assert panel.stale_notice() == ""

    panel.state().set_settings(replace(TemplateSettings(), video_bitrate_kbps=1500))
    panel.refresh()

    assert "run the dry run again" in panel.stale_notice()
    assert panel.state().rows()[0].video_bitrate_kbps == 1500


def test_summary_reacts_to_edits_and_empty_state(panel):
    assert panel.summary_text() == "No dry run yet"

    panel.set_report(
        make_report([make_scan_row(), make_scan_row("2026-06-Ride/B.MP4")]),
        TemplateSettings(),
    )
    assert "201.6 MB" in panel.summary_text()

    panel.state().set_bitrate("2026-06-Ride/B.MP4", 600)
    panel.refresh()
    assert "179.1 MB" in panel.summary_text()
