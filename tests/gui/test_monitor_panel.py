"""Monitor panel: tables, logs, summary, and stop/resume actions (issue #9)."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt

from gui.monitor_support import (
    ACTIVE_PATH,
    FakeSession,
    make_idle_worker,
    make_snapshot,
    make_worker,
)
from gui.monitor_support_qt import FakeMonitorRunner, dispose
from videocompressor.controller.protocol import ControllerError
from videocompressor.gui.monitor_panel import MonitorPanel


def _panel(session: FakeSession, runner: FakeMonitorRunner) -> MonitorPanel:
    return MonitorPanel(
        session,  # type: ignore[arg-type]
        runner=runner,  # type: ignore[arg-type]
        description="hosting controller at http://127.0.0.1:8765",
    )


def _cell(model, row: int, column: int) -> str:
    return model.data(model.index(row, column), Qt.ItemDataRole.DisplayRole)


def test_panel_starts_polling_and_renders_a_snapshot(qapp):
    session = FakeSession(make_snapshot(workers=[make_worker(), make_idle_worker()]))
    runner = FakeMonitorRunner()
    panel = _panel(session, runner)
    try:
        assert runner.started == 1

        panel.apply_snapshot(session.snapshot())

        assert panel.nodes_model.rowCount() == 2
        assert panel.jobs_model.rowCount() == 1
        assert _cell(panel.nodes_model, 0, 0) == "homeserver"
        assert _cell(panel.nodes_model, 0, 1) == "online"
        assert _cell(panel.nodes_model, 0, 2) == "12s ago"
        assert _cell(panel.nodes_model, 0, 3) == ACTIVE_PATH
        assert _cell(panel.nodes_model, 0, 4) == "archive → /srv/archive"
        assert _cell(panel.nodes_model, 0, 5) == "-"
        assert _cell(panel.nodes_model, 1, 1) == "offline"
        assert _cell(panel.jobs_model, 0, 2) == "encoding"
        assert _cell(panel.jobs_model, 0, 3) == "2"
        assert _cell(panel.jobs_model, 0, 4) == "41.2%"
        assert _cell(panel.jobs_model, 0, 5) == "38.5 fps · 0.74x"
        assert _cell(panel.jobs_model, 0, 6) == "20m 34s"
        assert panel.summary_label.text() == "completed 3 · skipped 12 · failed 0 · review-needed 1"
        assert "claimed GS010001.MP4" in panel.run_log.toPlainText()
        assert "hosting controller" in panel.description_label.text()
    finally:
        panel.shutdown()
        dispose(panel)
    assert runner.stopped == 1


def test_selection_enables_stop_and_filters_the_job_log(qapp):
    session = FakeSession(make_snapshot(workers=[make_worker(), make_idle_worker()]))
    panel = _panel(session, FakeMonitorRunner())
    try:
        panel.apply_snapshot(session.snapshot())

        panel.nodes_view.selectRow(0)
        assert panel.selected_worker() == "homeserver"
        assert panel.stop_button.isEnabled()
        assert panel.drain_button.isEnabled()
        assert not panel.resume_button.isEnabled()
        assert ACTIVE_PATH in panel.job_log.toPlainText()
        assert "claimed GS010001.MP4" in panel.job_log.toPlainText()

        panel.nodes_view.selectRow(1)
        assert panel.selected_worker() == "macbook"
        assert panel.stop_button.isEnabled()  # an idle node can be stopped too
        assert panel.drain_button.isEnabled()
        assert not panel.resume_button.isEnabled()
    finally:
        panel.shutdown()
        dispose(panel)


def test_stop_and_resume_call_the_session_and_refresh(qapp):
    session = FakeSession(make_snapshot(workers=[make_worker()]))
    runner = FakeMonitorRunner()
    panel = _panel(session, runner)
    try:
        panel.apply_snapshot(session.snapshot())
        panel.nodes_view.selectRow(0)

        panel.stop_button.click()
        assert session.stops == [("homeserver", "cancel")]
        assert runner.refreshes == 1
        assert "stop now requested for homeserver" in panel.status_label.text()

        panel.apply_snapshot(make_snapshot(workers=[make_worker(stop_requested=True)]))
        panel.nodes_view.selectRow(0)
        assert _cell(panel.nodes_model, 0, 5) == "stopping…"
        assert not panel.stop_button.isEnabled()
        assert not panel.drain_button.isEnabled()
        assert panel.resume_button.isEnabled()
        panel.resume_button.click()
        assert session.resumes == ["homeserver"]
        assert runner.refreshes == 2
    finally:
        panel.shutdown()
        dispose(panel)


def test_drain_action_works_for_an_idle_node(qapp):
    session = FakeSession(make_snapshot(workers=[make_idle_worker()]))
    runner = FakeMonitorRunner()
    panel = _panel(session, runner)
    try:
        panel.apply_snapshot(session.snapshot())
        panel.nodes_view.selectRow(0)

        panel.drain_button.click()
        assert session.stops == [("macbook", "drain")]
        assert runner.refreshes == 1
        assert "stop after current requested for macbook" in panel.status_label.text()

        panel.apply_snapshot(
            make_snapshot(
                workers=[make_idle_worker(stop_requested=True, stop_mode="drain")]
            )
        )
        panel.nodes_view.selectRow(0)
        assert _cell(panel.nodes_model, 0, 5) == "draining…"
    finally:
        panel.shutdown()
        dispose(panel)


def test_action_failures_go_to_the_status_line(qapp):
    session = FakeSession(
        make_snapshot(workers=[make_worker()]),
        stop_error=ControllerError(404, "unknown_worker", "worker 'homeserver' is not registered"),
    )
    panel = _panel(session, FakeMonitorRunner())
    try:
        panel.apply_snapshot(session.snapshot())
        panel.nodes_view.selectRow(0)
        panel.stop_selected()
        assert panel.status_label.text() == (
            "cannot stop homeserver: worker 'homeserver' is not registered"
        )
    finally:
        panel.shutdown()
        dispose(panel)


def test_runner_signals_drive_the_panel(qapp):
    panel = _panel(FakeSession(), FakeMonitorRunner())
    try:
        panel.runner.emit_updated(make_snapshot(workers=[make_worker()]))
        assert panel.nodes_model.rowCount() == 1

        panel.runner.emit_failed("controller unreachable at http://127.0.0.1:8765")
        assert "unreachable" in panel.status_label.text()
    finally:
        panel.shutdown()
        dispose(panel)


def test_has_active_jobs_tracks_the_snapshot(qapp):
    panel = _panel(FakeSession(), FakeMonitorRunner())
    try:
        assert panel.has_active_jobs() is False
        panel.apply_snapshot(make_snapshot(workers=[make_worker()]))
        assert panel.has_active_jobs() is True
        panel.apply_snapshot(make_snapshot(workers=[make_idle_worker()]))
        assert panel.has_active_jobs() is False
    finally:
        panel.shutdown()
        dispose(panel)
