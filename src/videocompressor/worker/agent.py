"""Worker agent: register, heartbeat, claim, and run one job at a time (issue #6).

The agent owns the node's lifecycle: it registers with the controller, keeps a
15 s heartbeat thread (renewing the worker lease and, while a job runs, the job
lease and progress), claims one queued clip at a time, and reports the terminal
state. A heartbeat stop carries a mode (issue #74): ``cancel`` cancels the
active job and exits the loop, ``drain`` lets the active job settle and exits
without claiming again; both also exit an idle node, and a missing mode means
cancel. SIGTERM/SIGINT cancels the job and exits (``request_stop``); SIGUSR1
requests a drain. Cancelling kills the active FFmpeg process through the
pipeline's stop event and discards the local work; the clip is requeued and
restarts from the beginning later.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from videocompressor import __version__
from videocompressor.core.ffmpeg import EncodeProgress
from videocompressor.core.manifest import HEARTBEAT_INTERVAL_SECONDS
from videocompressor.core.models import StopMode
from videocompressor.worker.client import (
    ClaimedJob,
    ControllerAuthError,
    ControllerClient,
    ControllerError,
    ControllerUnavailable,
    LeaseConflict,
)
from videocompressor.worker.config import WorkerConfig
from videocompressor.worker.pipeline import JobOutcome, JobPipeline
from videocompressor.worker.scratch import prepare_scratch_root

__all__ = ["WorkerAgent"]

_log = logging.getLogger("videocompressor.worker")

#: Backoff before retrying a rejected/unreachable controller request.
RETRY_BACKOFF_S = 5.0

#: Terminal-report attempts before giving up (the on-disk state stays recoverable).
REPORT_ATTEMPTS = 3


class WorkerAgent:
    """The register/heartbeat/claim loop for one processing node."""

    def __init__(
        self,
        config: WorkerConfig,
        *,
        client: ControllerClient | None = None,
        pipeline: JobPipeline | None = None,
        version: str = __version__,
    ) -> None:
        self.config = config
        self.version = version
        self.client = client if client is not None else ControllerClient(
            config.controller_url, config.token
        )
        self._shutdown = threading.Event()
        self._cancel_job = threading.Event()
        self._drain = threading.Event()
        self._heartbeat_stop = threading.Event()
        self._wake_heartbeat = threading.Event()
        self._progress_lock = threading.Lock()
        self._active: ClaimedJob | None = None
        self._progress: dict[str, Any] = {}
        self._heartbeat_interval = config.heartbeat_seconds or float(HEARTBEAT_INTERVAL_SECONDS)
        if pipeline is not None:
            self.pipeline = pipeline
        else:
            self.pipeline = JobPipeline(
                config, on_phase=self._on_phase, on_progress=self._on_progress
            )

    # -- lifecycle ---------------------------------------------------------

    def register(self) -> None:
        """Register (or refresh) this node and adopt the controller's cadence."""
        response = self.client.register(
            worker_id=self.config.worker_id,
            name=self.config.name or self.config.worker_id,
            roots={root_id: str(path) for root_id, path in self.config.roots.items()},
            scratch_dir=str(self.config.scratch_dir),
            thread_cap=self.config.thread_cap,
            version=self.version,
        )
        seconds = response.get("heartbeat_seconds")
        if (
            self.config.heartbeat_seconds is None
            and isinstance(seconds, (int, float))
            and not isinstance(seconds, bool)
            and seconds > 0
        ):
            self._heartbeat_interval = float(seconds)
        _log.info(
            "registered worker %s with controller %s",
            self.config.worker_id,
            self.config.controller_url,
        )

    def run(self, *, max_jobs: int | None = None) -> int:
        """Sweep scratch, register, and claim/process jobs until stopped.

        ``max_jobs=1`` (``vc worker --once``) exits immediately when nothing is
        claimable; otherwise the loop polls and runs until ``request_stop()``.
        """
        prepare_scratch_root(self.config.scratch_dir)
        self.register()
        heartbeat = threading.Thread(
            target=self._heartbeat_loop, name="vc-worker-heartbeat", daemon=True
        )
        heartbeat.start()
        processed = 0
        try:
            while not self._shutdown.is_set() and (max_jobs is None or processed < max_jobs):
                if self._drain.is_set():
                    _log.info("draining: exiting without claiming another job")
                    break
                job = self._try_claim()
                if job is None:
                    if max_jobs is not None:
                        break  # --once: nothing claimable right now
                    self._shutdown.wait(self.config.poll_seconds)
                    continue
                self._run_job(job)
                processed += 1
        finally:
            self._heartbeat_stop.set()
            self._wake_heartbeat.set()
            heartbeat.join(timeout=5)
        return 0

    def request_stop(self) -> None:
        """Cancel the active job (if any) and exit the claim loop."""
        self._shutdown.set()
        self._cancel_job.set()
        self._wake_heartbeat.set()

    def request_drain(self) -> None:
        """Finish the active job normally, then exit; exit immediately when idle."""
        self._drain.set()
        self._wake_heartbeat.set()
        with self._progress_lock:
            idle = self._active is None
        if idle:
            self._shutdown.set()

    # -- claiming and running ---------------------------------------------

    def _try_claim(self) -> ClaimedJob | None:
        while not self._shutdown.is_set():
            try:
                return self.client.claim(self.config.worker_id)
            except ControllerAuthError:
                raise
            except LeaseConflict as exc:
                _log.warning("claim conflict: %s", exc)
            except ControllerError as exc:
                if exc.code == "unknown_worker":
                    self._reregister_quietly()
                else:
                    _log.warning("claim rejected: %s", exc)
            except ControllerUnavailable as exc:
                _log.warning("controller unreachable during claim: %s", exc)
            self._shutdown.wait(RETRY_BACKOFF_S)
        return None

    def _run_job(self, job: ClaimedJob) -> JobOutcome:
        self._cancel_job.clear()
        with self._progress_lock:
            self._active = job
            self._progress = {"phase": "claimed"}
        self._poke_heartbeat()
        try:
            try:
                outcome = self.pipeline.run(job, stop_event=self._cancel_job)
            except Exception as exc:  # one bad job must never kill the node
                _log.exception("job %s failed unexpectedly", job.relative_path)
                outcome = JobOutcome("failed", f"internal error: {exc}")
            self._report(job, outcome)
        finally:
            with self._progress_lock:
                self._active = None
                self._progress = {}
        _log.info(
            "job %s settled: %s%s",
            job.relative_path,
            outcome.disposition,
            f" ({outcome.detail})" if outcome.detail else "",
        )
        return outcome

    def _report(self, job: ClaimedJob, outcome: JobOutcome) -> None:
        if outcome.disposition == "complete" and outcome.output is None:
            outcome = JobOutcome("failed", "internal error: complete outcome without a fingerprint")
        for attempt in range(REPORT_ATTEMPTS):
            try:
                if outcome.disposition == "complete":
                    assert outcome.output is not None  # normalized above
                    self.client.complete(self.config.worker_id, job, outcome.output)
                else:
                    self.client.fail(
                        self.config.worker_id,
                        job,
                        error=outcome.detail or outcome.disposition,
                        disposition=outcome.disposition,
                    )
                return
            except LeaseConflict as exc:
                _log.warning("job %s already settled elsewhere; dropping report (%s)",
                             job.relative_path, exc)
                return
            except ControllerAuthError:
                raise
            except (ControllerError, ControllerUnavailable) as exc:
                if attempt + 1 >= REPORT_ATTEMPTS:
                    _log.error(
                        "could not report %s (%s): %s",
                        job.relative_path,
                        outcome.disposition,
                        exc,
                    )
                    return
                self._shutdown.wait(RETRY_BACKOFF_S)

    # -- heartbeat ---------------------------------------------------------

    def _heartbeat_loop(self) -> None:
        while not self._heartbeat_stop.is_set():
            self._wake_heartbeat.wait(self._heartbeat_interval)
            self._wake_heartbeat.clear()
            if self._heartbeat_stop.is_set():
                break
            self._heartbeat_once()

    def _heartbeat_once(self) -> None:
        with self._progress_lock:
            active_job = self._active
            progress = dict(self._progress) if self._progress else None
        active = (active_job.root_id, active_job.relative_path) if active_job is not None else None
        try:
            response = self.client.heartbeat(
                self.config.worker_id, active=active, progress=progress
            )
        except LeaseConflict as exc:
            _log.warning(
                "heartbeat lost the lease on %s: %s",
                active_job.relative_path if active_job is not None else "(idle)",
                exc,
            )
            if active_job is not None:
                self._cancel_job.set()
        except ControllerError as exc:
            if exc.code == "unknown_worker":
                self._reregister_quietly()
            else:
                _log.warning("heartbeat rejected: %s", exc)
        except ControllerUnavailable as exc:
            _log.warning("controller unreachable during heartbeat: %s", exc)
        else:
            if response.get("stop"):
                mode = response.get("stop_mode") or StopMode.CANCEL
                if mode == StopMode.DRAIN:
                    _log.info("drain requested for %s", self.config.worker_id)
                    self._drain.set()
                    if active_job is None:
                        self._shutdown.set()
                else:
                    _log.info("stop requested for %s", self.config.worker_id)
                    self._cancel_job.set()
                    self._shutdown.set()

    def _reregister_quietly(self) -> None:
        try:
            self.register()
            _log.info("re-registered worker %s after controller restart", self.config.worker_id)
        except (ControllerError, ControllerUnavailable) as exc:
            _log.warning("re-registration failed: %s", exc)

    def _poke_heartbeat(self) -> None:
        self._wake_heartbeat.set()

    # -- progress callbacks ------------------------------------------------

    def _on_phase(self, phase: str) -> None:
        with self._progress_lock:
            self._progress = {**self._progress, "phase": phase}
        self._poke_heartbeat()

    def _on_progress(self, progress: EncodeProgress) -> None:
        payload: dict[str, Any] = {"phase": "encoding", "pass": progress.pass_number}
        if progress.percent is not None:
            payload["percent"] = round(progress.percent, 1)
        if progress.fps is not None:
            payload["fps"] = round(progress.fps, 1)
        if progress.speed is not None:
            payload["speed"] = f"{progress.speed:.2f}x"
        if progress.eta_seconds is not None:
            payload["eta_seconds"] = int(progress.eta_seconds)
        with self._progress_lock:
            self._progress = payload
        self._poke_heartbeat()
