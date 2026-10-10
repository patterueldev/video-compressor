#!/usr/bin/env python3
"""Shared helpers for the issue #13 rerun-matrix check.

The matrix tooling lives in two sibling modules (loaded like ``pilot_common``,
with the ``scripts`` directory on ``sys.path``):

- :mod:`matrix_common` (this module): staging, fault injection, the controller
  and worker-subprocess helpers, lease expiry, and the sanitized report;
- ``rerun_matrix.py``: the CLI and the four scenarios.

Everything the matrix writes lives under the caller's ``--matrix-root`` (plus
the optional ``--report``), and every emitted report is sanitized: absolute
archive/matrix/scratch prefixes are replaced with placeholders.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from datetime import time as dt_time
from pathlib import Path, PurePosixPath
from typing import Any
from zoneinfo import ZoneInfo

from pilot_common import (
    ARCHIVE_PLACEHOLDER,
    SCRATCH_PLACEHOLDER,
    ToolError,
    sanitize_text,
)

from videocompressor.controller.server import ControllerServer
from videocompressor.controller.store import ControllerStore
from videocompressor.core.capture_time import resolve_capture_time
from videocompressor.core.config import DEFAULT_TEMPLATE_NAME
from videocompressor.core.ffmpeg import estimate_output_bytes, required_scratch_bytes
from videocompressor.core.fingerprint import fingerprint_confirms, fingerprint_file
from videocompressor.core.manifest import Manifest
from videocompressor.core.models import (
    CaptureTime,
    CaptureTimeMode,
    Classification,
    ClipRecord,
    Fingerprint,
    JobState,
    TemplateSettings,
)
from videocompressor.core.probe import FFprobeUnavailableError, ProbeError, probe_media

__all__ = [
    "ACTIVE_STATES", "HEARTBEAT_SECONDS", "MATRIX_PLACEHOLDER", "POLL_SECONDS", "ROOT_ID",
    "TERMINAL_STATES", "Check", "MatrixContext", "RunningWorker", "ScenarioReport",
    "build_report", "capture_time_for", "clip_path", "expire_lease", "generate_clip",
    "mutate_clip", "preflight_scratch", "report_replacements", "requeue", "run_worker_once",
    "seed_queued", "stage_clip", "start_worker", "stop_all_workers", "stop_worker",
    "wait_for_state", "wait_for_terminal", "worker_env", "write_failing_ffmpeg_shim",
    "write_report",
]

MATRIX_PLACEHOLDER = "<matrix-root>"
ROOT_ID = "matrix"
HEARTBEAT_SECONDS = 0.5
POLL_SECONDS = 0.1

#: States a worker holds while a matrix scenario can interrupt it pre-replacement.
ACTIVE_STATES = frozenset(
    {
        JobState.CLAIMED,
        JobState.COPIED,
        JobState.ENCODING,
        JobState.VALIDATED,
        JobState.TRANSFERRING,
        JobState.REPLACING,
    }
)
TERMINAL_STATES = frozenset({JobState.COMPLETE, JobState.FAILED, JobState.REVIEW})

#: Fixed fallback capture time (daytime, template timezone) when a clip has no
#: usable creation_time; the matrix does not test capture-time interpretation.
FALLBACK_CAPTURE_DATE = datetime(2026, 6, 1).date()
FALLBACK_CAPTURE_TIME = dt_time(12, 0)


@dataclass(frozen=True)
class Check:
    """One PASS/FAIL assertion in a scenario transcript."""

    label: str
    ok: bool
    detail: str = ""


@dataclass
class ScenarioReport:
    """Collected checks for one matrix scenario."""

    scenario_id: str
    title: str
    checks: list[Check] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        """Whether every recorded check passed (an empty scenario fails)."""
        return bool(self.checks) and all(check.ok for check in self.checks)

    def check(self, label: str, ok: object, detail: str = "") -> bool:
        """Record and print one assertion; returns ``ok`` for convenient guards."""
        passed = bool(ok)
        self.checks.append(Check(label=label, ok=passed, detail=detail))
        status = "PASS" if passed else "FAIL"
        suffix = f" ({detail})" if detail else ""
        print(f"  [{status}] {label}{suffix}")
        return passed

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form for the evidence report."""
        return {
            "id": self.scenario_id,
            "title": self.title,
            "ok": self.ok,
            "seconds": self.seconds,
            "checks": [
                {"label": check.label, "ok": check.ok, "detail": check.detail}
                for check in self.checks
            ],
        }


@dataclass
class RunningWorker:
    """One worker subprocess with its log destination."""

    process: subprocess.Popen
    log_path: Path
    log_handle: Any


@dataclass
class MatrixContext:
    """Everything one matrix run needs after the controller is up."""

    archive_dir: Path
    scratch_dir: Path
    state_dir: Path
    logs_dir: Path
    matrix_root: Path
    manifest: Manifest
    store: ControllerStore
    server: ControllerServer
    base_url: str
    token: str
    synthetic: bool
    timeout: float
    clips: tuple[str, ...] = ()
    staged: dict[str, str] = field(default_factory=dict)
    extra_new: str = ""
    template: TemplateSettings = field(default_factory=TemplateSettings)
    staging: list[dict[str, Any]] = field(default_factory=list)
    workers: list[RunningWorker] = field(default_factory=list)


def clip_path(ctx: MatrixContext, relative_path: str) -> Path:
    """Resolve an archive-relative matrix path under the matrix archive."""
    return ctx.archive_dir.joinpath(*PurePosixPath(relative_path).parts)


# -- staging, mutation, and fault injection -----------------------------------


def generate_clip(path: Path, seconds: int) -> None:
    """Generate one synthetic test clip with video, audio, and a capture time."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", f"testsrc2=size=640x360:rate=15:duration={seconds}",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
            "-c:v", "mpeg4", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
            "-metadata", "creation_time=2026-06-01T12:00:00Z",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def stage_clip(source: Path, destination: Path) -> tuple[int, float]:
    """Copy ``source`` to ``destination`` and verify size + sampled digest.

    Returns ``(bytes, seconds)`` so the report can carry staging throughput.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    shutil.copyfile(source, destination)
    elapsed = max(time.monotonic() - started, 1e-9)
    expected = fingerprint_file(source)
    actual = fingerprint_file(destination)
    if not fingerprint_confirms(expected, actual):
        raise ToolError(f"staged copy of {source} did not verify")
    return actual.size_bytes, elapsed


def mutate_clip(clip: Path, label: str) -> Fingerprint:
    """Remux ``clip`` with new metadata in place; returns the new fingerprint."""
    before = fingerprint_file(clip)
    variant = clip.with_name(f"{clip.stem}.{label}.variant{clip.suffix}")
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-v", "error",
                "-i", str(clip),
                "-c", "copy",
                "-metadata", f"comment=rerun-matrix-{label}",
                str(variant),
            ],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        variant.unlink(missing_ok=True)
        raise ToolError(f"could not remux {clip.name}: {exc}") from exc
    try:
        probe_media(variant)
    except (ProbeError, FFprobeUnavailableError) as exc:
        variant.unlink(missing_ok=True)
        raise ToolError(f"mutated {clip.name} is not readable media: {exc}") from exc
    os.replace(variant, clip)
    after = fingerprint_file(clip)
    if after == before:
        raise ToolError(f"mutation of {clip.name} did not change the fingerprint")
    return after


def write_failing_ffmpeg_shim(directory: Path) -> Path:
    """Write an ``ffmpeg`` wrapper that fails only on ``-pass 2`` (fault injection)."""
    real = shutil.which("ffmpeg")
    if real is None:
        raise ToolError("ffmpeg is required on PATH")
    directory.mkdir(parents=True, exist_ok=True)
    shim = directory / "ffmpeg"
    shim.write_text(
        "#!/bin/sh\n"
        'prev=""\n'
        'for arg in "$@"; do\n'
        '  if [ "$prev" = "-pass" ] && [ "$arg" = "2" ]; then\n'
        '    echo "injected encode failure (rerun-matrix shim)" >&2\n'
        "    exit 7\n"
        "  fi\n"
        '  prev="$arg"\n'
        "done\n"
        f"exec {shlex.quote(real)} \"$@\"\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return shim


# -- queue seeding and lease expiry -------------------------------------------


def capture_time_for(path: Path, settings: TemplateSettings) -> CaptureTime:
    """Resolve a clip's capture time, falling back to a fixed daytime instant."""
    try:
        media = probe_media(path)
        capture = resolve_capture_time(
            media, mode=CaptureTimeMode.LOCAL_WALL_CLOCK, timezone=settings.timezone
        )
        if capture.when is not None and capture.issue is None:
            return capture
    except (ProbeError, FFprobeUnavailableError, ValueError):
        pass
    when = datetime.combine(
        FALLBACK_CAPTURE_DATE, FALLBACK_CAPTURE_TIME, tzinfo=ZoneInfo(settings.timezone)
    )
    return CaptureTime(when=when, mode=CaptureTimeMode.LOCAL_WALL_CLOCK)


def seed_queued(ctx: MatrixContext, relative_path: str, clip: Path) -> None:
    """Insert (or replace) the clip as a queued job with a fresh fingerprint."""
    now = datetime.now(UTC)
    record = ClipRecord(
        root_id=ROOT_ID,
        relative_path=relative_path,
        source=fingerprint_file(clip),
        state=JobState.QUEUED,
        classification=Classification.NEW,
        capture_time=capture_time_for(clip, ctx.template),
        template_id=DEFAULT_TEMPLATE_NAME,
        created_at=now,
        updated_at=now,
    )
    ctx.manifest.upsert_clip(record)


def requeue(ctx: MatrixContext, relative_path: str, clip: Path) -> None:
    """Return a settled clip to the queue with its current fingerprint."""
    ctx.manifest.update_state(
        ROOT_ID,
        relative_path,
        JobState.QUEUED,
        source=fingerprint_file(clip),
        error=None,
        completed_at=None,
    )


def expire_lease(ctx: MatrixContext, relative_path: str) -> None:
    """Backdate the clip's lease and run one sweep, as a real timeout would.

    ``Manifest`` exposes no lease-expiry API; backdating the matrix's own queue
    database keeps the interruption scenario deterministic instead of waiting
    out the 90 s TTL. The report records that the expiry was accelerated.
    """
    past = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    ctx.manifest._connection.execute(
        "UPDATE clips SET lease_expires_at = ? WHERE root_id = ? AND relative_path = ?",
        (past, ROOT_ID, relative_path),
    )
    ctx.server.sweep_once()


def wait_for_state(
    ctx: MatrixContext, relative_path: str, states: frozenset[JobState] | set[JobState]
) -> JobState | None:
    """Poll the manifest until the clip is in one of ``states`` (or timeout)."""
    deadline = time.monotonic() + ctx.timeout
    while time.monotonic() < deadline:
        record = ctx.manifest.get_clip(ROOT_ID, relative_path)
        if record is not None and record.state in states:
            return record.state
        time.sleep(POLL_SECONDS)
    return None


def wait_for_terminal(ctx: MatrixContext, relative_path: str) -> ClipRecord | None:
    """Poll the manifest until the clip reaches a terminal state."""
    deadline = time.monotonic() + ctx.timeout
    while time.monotonic() < deadline:
        record = ctx.manifest.get_clip(ROOT_ID, relative_path)
        if record is not None and record.state in TERMINAL_STATES:
            return record
        time.sleep(POLL_SECONDS)
    return None


# -- worker subprocesses ------------------------------------------------------


def worker_env(ctx: MatrixContext, *, extra_path: Path | None = None) -> dict[str, str]:
    """Environment for a matrix worker; optionally prepend a PATH directory."""
    env = {**os.environ, "VC_WORKER_TOKEN": ctx.token}
    if extra_path is not None:
        env["PATH"] = f"{extra_path}{os.pathsep}{env.get('PATH', '')}"
    return env


def write_worker_config(ctx: MatrixContext, worker_id: str) -> Path:
    """Write the per-worker TOML (token by env; scratch/roots under the matrix)."""
    scratch = ctx.scratch_dir / worker_id
    scratch.mkdir(parents=True, exist_ok=True)
    path = ctx.state_dir / f"node-{worker_id}.toml"
    path.write_text(
        "[worker]\n"
        f'id = "{worker_id}"\n'
        f'controller_url = "{ctx.base_url}"\n'
        'token_env = "VC_WORKER_TOKEN"\n'
        f'scratch_dir = "{scratch}"\n'
        f"heartbeat_seconds = {HEARTBEAT_SECONDS}\n"
        "poll_seconds = 0.2\n"
        "thread_cap = 1\n"
        "\n"
        "[roots]\n"
        f'{ROOT_ID} = "{ctx.archive_dir}"\n',
        encoding="utf-8",
    )
    return path


def start_worker(
    ctx: MatrixContext,
    worker_id: str,
    *,
    log_name: str,
    once: bool = False,
    env: dict[str, str] | None = None,
) -> RunningWorker:
    """Start one ``vc worker`` subprocess in its own session (killable group)."""
    config_path = write_worker_config(ctx, worker_id)
    command = [
        sys.executable, "-m", "videocompressor.cli", "worker", "--config", str(config_path)
    ]
    if once:
        command.append("--once")
    log_path = ctx.logs_dir / f"{log_name}.log"
    handle = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        command,
        stdout=handle,
        stderr=subprocess.STDOUT,
        text=True,
        env=env if env is not None else worker_env(ctx),
        start_new_session=True,
    )
    worker = RunningWorker(process=process, log_path=log_path, log_handle=handle)
    ctx.workers.append(worker)
    return worker


def stop_worker(worker: RunningWorker, *, kill: bool = False) -> None:
    """Stop one worker subprocess (optionally SIGKILL its whole process group)."""
    process = worker.process
    if process.poll() is None:
        try:
            if kill:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            else:
                process.terminate()
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
    worker.log_handle.close()


def stop_all_workers(ctx: MatrixContext) -> None:
    """Best-effort cleanup of every worker the scenario run started."""
    for worker in list(ctx.workers):
        try:
            stop_worker(worker, kill=True)
        except Exception:  # cleanup must never mask the scenario result
            pass
    ctx.workers.clear()


def run_worker_once(
    ctx: MatrixContext,
    worker_id: str,
    *,
    log_name: str,
    env: dict[str, str] | None = None,
) -> tuple[int, str]:
    """Run ``vc worker --once`` to completion; returns (exit code, log text)."""
    worker = start_worker(ctx, worker_id, log_name=log_name, once=True, env=env)
    try:
        try:
            code = worker.process.wait(timeout=ctx.timeout)
        except subprocess.TimeoutExpired as exc:
            raise ToolError(f"worker {worker_id} timed out after {ctx.timeout:g}s") from exc
    finally:
        stop_worker(worker, kill=True)
    return code, worker.log_path.read_text(encoding="utf-8", errors="replace")


def preflight_scratch(ctx: MatrixContext) -> None:
    """Fail early when scratch cannot hold the largest staged clip plus margin."""
    largest = max(ctx.staging, key=lambda entry: entry["staged_bytes"])
    estimated = 0
    try:
        media = probe_media(clip_path(ctx, largest["relative_path"]))
        if media.duration_s is not None:
            estimated = estimate_output_bytes(media.duration_s, 800, 96)
    except (ProbeError, FFprobeUnavailableError):
        pass
    required = required_scratch_bytes(
        source_bytes=largest["staged_bytes"], estimated_output_bytes=estimated
    )
    available = shutil.disk_usage(ctx.scratch_dir).free
    print(f"scratch: need {required} bytes, {available} available under {ctx.matrix_root}")
    if available < required:
        raise ToolError(
            f"insufficient scratch space under {ctx.matrix_root}: "
            f"need {required} bytes, {available} available"
        )


# -- evidence report ----------------------------------------------------------


def tool_version(binary: str) -> str:
    """First version line of a binary, or ``unavailable``."""
    try:
        result = subprocess.run([binary, "-version"], capture_output=True, text=True)
    except OSError:
        return "unavailable"
    return (result.stdout.splitlines() or ["unavailable"])[0]


def build_report(
    *,
    synthetic: bool,
    archive_root: Path | None,
    matrix_root: Path,
    staging: list[dict[str, Any]],
    reports: list[ScenarioReport],
    manifest: Manifest,
) -> dict[str, Any]:
    """Build the evidence document (absolute paths are sanitized by the writer)."""
    records = {record.relative_path: record for record in manifest.list_clips()}
    clips = []
    for entry in staging:
        record = records.get(entry["relative_path"])
        clips.append(
            {
                **entry,
                "state": str(record.state) if record is not None else None,
                "output_bytes": (
                    record.output.size_bytes
                    if record is not None and record.output is not None
                    else None
                ),
            }
        )
    checks = sum(len(report.checks) for report in reports)
    failures = sum(1 for report in reports for check in report.checks if not check.ok)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": "synthetic" if synthetic else "archive",
        "archive_root": str(archive_root) if archive_root is not None else None,
        "matrix_root": str(matrix_root),
        "clips": clips,
        "scenarios": [report.to_dict() for report in reports],
        "totals": {
            "scenarios": len(reports),
            "failed_scenarios": sum(1 for report in reports if not report.ok),
            "checks": checks,
            "failures": failures,
        },
        "tools": {"ffmpeg": tool_version("ffmpeg"), "ffprobe": tool_version("ffprobe")},
    }


def write_report(
    document: dict[str, Any], path: Path, replacements: dict[str, str]
) -> Path:
    """Write the report as sanitized JSON; returns the written path."""
    text = sanitize_text(json.dumps(document, indent=2, sort_keys=True), replacements)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8")
    return path


def report_replacements(ctx: MatrixContext, archive_root: Path | None) -> dict[str, str]:
    """Sanitization map for the report and transcript placeholders."""
    replacements: dict[str, str] = {
        str(ctx.matrix_root.resolve()): MATRIX_PLACEHOLDER,
        str(ctx.scratch_dir.resolve()): SCRATCH_PLACEHOLDER,
    }
    if archive_root is not None:
        replacements[str(archive_root.resolve())] = ARCHIVE_PLACEHOLDER
    return replacements
