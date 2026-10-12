"""Offscreen Qt tests for the local-worker control strip (issue #52)."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from gui.local_worker_support import make_config
from gui.monitor_support import make_snapshot, make_worker
from gui.monitor_support_qt import dispose
from videocompressor.core.config import load_config
from videocompressor.gui.local_worker import (
    STATE_ALREADY_RUNNING,
    STATE_STARTED_CHILD,
    STATE_STOPPED,
    LocalWorkerOutcome,
)
from videocompressor.gui.local_worker_panel import LocalWorkerPanel


class FakeSupervisor:
    """Records the panel's calls and answers with fixed outcomes."""

    def __init__(self, status: LocalWorkerOutcome | None = None) -> None:
        self.ensure_calls = 0
        self.stop_calls = 0
        self.refresh_calls = 0
        self.last_online: bool | None = None
        self._status = (
            status if status is not None else LocalWorkerOutcome(STATE_STOPPED, "stopped")
        )

    def ensure_started(self) -> LocalWorkerOutcome:
        self.ensure_calls += 1
        return LocalWorkerOutcome(STATE_STARTED_CHILD, "started the local worker child process")

    def stop(self) -> LocalWorkerOutcome:
        self.stop_calls += 1
        return LocalWorkerOutcome(STATE_STOPPED, "stopped")

    def refresh(self) -> LocalWorkerOutcome:
        self.refresh_calls += 1
        return LocalWorkerOutcome(STATE_STARTED_CHILD, "started the local worker child process")

    def status(self, *, online: bool | None = None) -> LocalWorkerOutcome:
        self.last_online = online
        if online:
            return LocalWorkerOutcome(STATE_ALREADY_RUNNING, "online (started outside the app)")
        return self._status


@pytest.fixture
def build_panel(qapp, tmp_path):
    """Build a panel per test and dispose every widget afterwards."""
    created = []

    def _build(**config_kwargs):
        config = make_config(tmp_path, **config_kwargs)
        supervisor = FakeSupervisor()
        panel = LocalWorkerPanel(supervisor, config, tmp_path / "config.toml")
        created.append(panel)
        return panel, supervisor, config

    yield _build
    for panel in created:
        dispose(panel)


def test_panel_renders_the_config_defaults(build_panel, tmp_path):
    panel, supervisor, _ = build_panel()

    assert panel.enabled_check.isChecked()
    assert not panel.keep_check.isChecked()
    assert panel.thread_spin.value() == 2
    assert panel.scratch_edit.text() == ""
    assert panel.scratch_edit.placeholderText() == str(tmp_path / "scratch")
    assert panel.status_label.text() == "stopped"
    assert supervisor.last_online is False
    assert str(tmp_path / "local-worker.toml") in panel.toolTip()


def test_save_persists_disabling_and_stops_the_worker(build_panel, tmp_path):
    panel, supervisor, _ = build_panel()
    panel.enabled_check.setChecked(False)

    assert panel.save() is True

    assert supervisor.stop_calls == 1
    assert supervisor.refresh_calls == 0
    assert load_config(tmp_path / "config.toml").local_worker.enabled is False
    assert panel.last_error is None


def test_save_persists_enabling_and_applies_it(build_panel, tmp_path):
    panel, supervisor, _ = build_panel(enabled=False)
    panel.enabled_check.setChecked(True)
    panel.keep_check.setChecked(True)
    panel.thread_spin.setValue(4)

    assert panel.save() is True

    assert supervisor.refresh_calls == 1
    assert supervisor.stop_calls == 0
    saved = load_config(tmp_path / "config.toml").local_worker
    assert saved.enabled is True
    assert saved.keep_running is True
    assert saved.thread_cap == 4
    assert panel.status_label.text() == "started the local worker child process"


def test_save_validation_failure_keeps_the_file_untouched(build_panel, tmp_path):
    panel, supervisor, _ = build_panel()
    panel.scratch_edit.setText("relative/scratch")

    assert panel.save() is False

    assert "absolute" in (panel.last_error or "")
    assert panel.error_label.text() == panel.last_error
    assert not (tmp_path / "config.toml").exists()
    assert supervisor.refresh_calls == 0
    assert supervisor.stop_calls == 0


def test_save_round_trips_a_custom_scratch_dir(build_panel, tmp_path):
    panel, _supervisor, _ = build_panel()
    scratch = tmp_path / "fast-disk"
    scratch.mkdir()
    panel.scratch_edit.setText(str(scratch))

    assert panel.save() is True

    assert load_config(tmp_path / "config.toml").local_worker.scratch_dir == scratch


def test_start_and_stop_render_the_supervisor_outcomes(build_panel):
    panel, supervisor, _ = build_panel()

    panel.start()
    assert supervisor.ensure_calls == 1
    assert panel.status_label.text() == "started the local worker child process"

    panel.stop()
    assert supervisor.stop_calls == 1
    assert panel.status_label.text() == "stopped"


def test_apply_snapshot_follows_the_configured_worker_id(build_panel):
    panel, supervisor, _ = build_panel()

    panel.apply_snapshot(make_snapshot(workers=[make_worker("local")]))
    assert supervisor.last_online is True
    assert panel.status_label.text() == "online (started outside the app)"

    panel.apply_snapshot(make_snapshot(workers=[make_worker("homeserver")]))
    assert supervisor.last_online is False
    assert panel.status_label.text() == "stopped"


def test_on_roots_changed_refreshes_the_worker(build_panel):
    panel, supervisor, _ = build_panel()

    panel.on_roots_changed()

    assert supervisor.refresh_calls == 1
    assert panel.status_label.text() == "started the local worker child process"
