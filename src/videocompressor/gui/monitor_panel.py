"""Monitor tab: nodes, active jobs, logs, stop/resume, and the run summary (issue #9).

The panel is a pure client of :class:`MonitorSession` (controller contract v1):
each snapshot fills the read-only tables, the summary line, and the two log
views; the Stop/Resume buttons act on the selected node. The runner polls in
the background so the GUI thread only renders immutable snapshots.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from videocompressor.controller.protocol import ControllerError
from videocompressor.gui.monitor_client import MonitorUnavailable, describe_monitor_error
from videocompressor.gui.monitor_runner import MonitorRunner
from videocompressor.gui.monitor_service import (
    LogEvent,
    MonitorSession,
    MonitorSnapshot,
    WorkerStatus,
    format_progress,
    format_summary,
)
from videocompressor.gui.monitor_tables import JobsModel, NodesModel

__all__ = ["MonitorPanel"]

_NO_JOB = "select a node with an active job"


class MonitorPanel(QWidget):
    """Watch and steer active runs over controller contract v1."""

    def __init__(
        self,
        session: MonitorSession,
        *,
        runner: MonitorRunner | None = None,
        description: str = "",
        header: QWidget | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.session = session
        self._snapshot: MonitorSnapshot | None = None
        self._header = header
        self._closed = False

        self.description_label = QLabel(description or "controller monitor")
        self.refresh_button = QPushButton("Refresh")
        self.summary_label = QLabel("no summary yet")
        self.status_label = QLabel("waiting for the controller…")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self.nodes_model = NodesModel(self)
        self.jobs_model = JobsModel(self)
        self.nodes_view = _table_view(self.nodes_model)
        self.jobs_view = _table_view(self.jobs_model)

        self.stop_button = QPushButton("Stop now")
        self.stop_button.setToolTip("Cancel the active clip (it is requeued) and stop the node")
        self.stop_button.setEnabled(False)
        self.drain_button = QPushButton("Stop after current")
        self.drain_button.setToolTip("Finish the current clip, then stop the node")
        self.drain_button.setEnabled(False)
        self.resume_button = QPushButton("Resume job")
        self.resume_button.setEnabled(False)

        self.run_log = _log_view()
        self.job_log = _log_view()

        self.runner = runner if runner is not None else MonitorRunner(self._fetch, parent=self)
        self.runner.updated.connect(self.apply_snapshot)
        self.runner.failed.connect(self.show_error)

        self._build_layout()
        self.nodes_view.selectionModel().selectionChanged.connect(self._update_selection)
        self.stop_button.clicked.connect(self.stop_selected)
        self.drain_button.clicked.connect(self.drain_selected)
        self.resume_button.clicked.connect(self.resume_selected)
        self.refresh_button.clicked.connect(self.runner.refresh_now)
        self.runner.start()

    # -- public API --------------------------------------------------------

    def set_description(self, text: str) -> None:
        """Update the controller-mode label."""
        self.description_label.setText(text)

    def refresh_now(self) -> None:
        """Force an immediate poll (the Refresh button and the convert flow's Start)."""
        self.runner.refresh_now()

    def apply_snapshot(self, snapshot: MonitorSnapshot) -> None:
        """Render one controller poll."""
        if self._closed:
            return
        self._snapshot = snapshot
        self.nodes_model.set_snapshot(snapshot)
        self.jobs_model.set_snapshot(snapshot)
        self.summary_label.setText(format_summary(snapshot.summary))
        self.status_label.setText(f"updated {snapshot.fetched_at:%H:%M:%S}")
        self._render_run_log(snapshot.events)
        self._update_selection()

    def selected_worker(self) -> str | None:
        """The worker id of the selected node row, if any."""
        indexes = self.nodes_view.selectionModel().selectedRows()
        if not indexes:
            return None
        row = indexes[0].row()
        if row < 0 or row >= self.nodes_model.rowCount():
            return None
        return self.nodes_model.worker_at(row).worker_id

    def stop_selected(self) -> None:
        """Ask the selected worker to stop now (its active job is requeued)."""
        worker_id = self.selected_worker()
        if worker_id is None:
            return
        try:
            self.session.stop_worker(worker_id, mode="cancel")
        except (ControllerError, MonitorUnavailable) as error:
            self.show_error(f"cannot stop {worker_id}: {describe_monitor_error(error)}")
            return
        self.status_label.setText(f"stop now requested for {worker_id}")
        self.runner.refresh_now()

    def drain_selected(self) -> None:
        """Ask the selected worker to finish its current clip, then stop."""
        worker_id = self.selected_worker()
        if worker_id is None:
            return
        try:
            self.session.stop_worker(worker_id, mode="drain")
        except (ControllerError, MonitorUnavailable) as error:
            self.show_error(f"cannot stop {worker_id}: {describe_monitor_error(error)}")
            return
        self.status_label.setText(f"stop after current requested for {worker_id}")
        self.runner.refresh_now()

    def resume_selected(self) -> None:
        """Clear a pending stop request for the selected worker."""
        worker_id = self.selected_worker()
        if worker_id is None:
            return
        try:
            self.session.resume_worker(worker_id)
        except (ControllerError, MonitorUnavailable) as error:
            self.show_error(f"cannot resume {worker_id}: {describe_monitor_error(error)}")
            return
        self.status_label.setText(f"resumed {worker_id}")
        self.runner.refresh_now()

    def show_error(self, message: str) -> None:
        """Show a poll or action failure without opening a dialog."""
        if self._closed:
            return
        self.status_label.setText(message)

    def has_active_jobs(self) -> bool:
        """True when the latest snapshot shows a node with an active clip."""
        if self._snapshot is None:
            return False
        return any(worker.active_path is not None for worker in self._snapshot.workers)

    def shutdown(self) -> None:
        """Stop polling; late results are ignored."""
        self._closed = True
        self.runner.stop()

    # -- internals ---------------------------------------------------------

    def _fetch(self) -> MonitorSnapshot:
        return self.session.snapshot()

    def _build_layout(self) -> None:
        header = QHBoxLayout()
        header.addWidget(self.description_label, 1)
        header.addWidget(self.refresh_button)
        summary = QHBoxLayout()
        summary.addWidget(self.summary_label, 1)
        summary.addWidget(self.status_label)

        tables = QSplitter(Qt.Orientation.Horizontal)
        tables.addWidget(self.nodes_view)
        tables.addWidget(self.jobs_view)

        actions = QHBoxLayout()
        actions.addWidget(self.stop_button)
        actions.addWidget(self.drain_button)
        actions.addWidget(self.resume_button)
        actions.addStretch(1)

        logs = QSplitter(Qt.Orientation.Horizontal)
        logs.addWidget(_log_pane("Run log", self.run_log))
        logs.addWidget(_log_pane("Job log", self.job_log))

        body = QSplitter(Qt.Orientation.Vertical)
        body.addWidget(tables)
        body.addWidget(logs)
        body.setStretchFactor(0, 3)
        body.setStretchFactor(1, 2)

        layout = QVBoxLayout(self)
        if self._header is not None:
            layout.addWidget(self._header)
        layout.addLayout(header)
        layout.addLayout(summary)
        layout.addWidget(body, 1)
        layout.addLayout(actions)

    def _update_selection(self) -> None:
        worker = self._worker_by_id(self.selected_worker())
        can_stop = worker is not None and not worker.stop_requested
        can_resume = worker is not None and worker.stop_requested
        self.stop_button.setEnabled(can_stop)
        self.drain_button.setEnabled(can_stop)
        self.resume_button.setEnabled(can_resume)
        self._render_job_log(worker)

    def _worker_by_id(self, worker_id: str | None) -> WorkerStatus | None:
        if worker_id is None or self._snapshot is None:
            return None
        for worker in self._snapshot.workers:
            if worker.worker_id == worker_id:
                return worker
        return None

    def _render_run_log(self, events: tuple[LogEvent, ...]) -> None:
        self.run_log.setPlainText("\n".join(self._format_event(event) for event in events))
        _scroll_to_end(self.run_log)

    def _render_job_log(self, worker: WorkerStatus | None) -> None:
        if worker is None or worker.active_path is None:
            self.job_log.setPlainText(_NO_JOB)
            return
        header = f"{worker.worker_id} · {worker.active_path}"
        progress = format_progress(worker.progress)
        if progress != "-":
            header += f" · {progress}"
        events = self.session.events_for(
            worker_id=worker.worker_id, relative_path=worker.active_path
        )
        lines = [header, ""]
        lines.extend(self._format_event(event) for event in events)
        self.job_log.setPlainText("\n".join(lines))
        _scroll_to_end(self.job_log)

    @staticmethod
    def _format_event(event: LogEvent) -> str:
        when = event.at.strftime("%H:%M:%S") if event.at is not None else "--:--:--"
        context = event.relative_path or event.worker_id or ""
        suffix = f" [{context}]" if context else ""
        return f"{when} [{event.level}] {event.message}{suffix}"


def _table_view(model: object) -> QTableView:
    view = QTableView()
    view.setModel(model)  # type: ignore[arg-type]
    view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    view.verticalHeader().setVisible(False)
    view.horizontalHeader().setStretchLastSection(True)
    return view


def _log_view() -> QPlainTextEdit:
    view = QPlainTextEdit()
    view.setReadOnly(True)
    view.setMaximumBlockCount(2000)
    return view


def _log_pane(title: str, view: QPlainTextEdit) -> QWidget:
    pane = QWidget()
    layout = QVBoxLayout(pane)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(QLabel(title))
    layout.addWidget(view, 1)
    return pane


def _scroll_to_end(view: QPlainTextEdit) -> None:
    scrollbar = view.verticalScrollBar()
    scrollbar.setValue(scrollbar.maximum())
