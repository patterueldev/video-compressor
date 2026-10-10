#!/usr/bin/env python3
"""Rerun-matrix hardening check for issue #13 (plan.md pilot step 7).

Runs four system-level scenarios against a real controller and worker
subprocesses and prints a PASS/FAIL transcript suitable for evidence:

- ``rerun_skip``: a completed clip still classifies complete after a template
  change, is never re-queued, and a rerun worker claims nothing;
- ``changed_review``: a completed clip whose file changes goes to ``changed``
  (never a silent replace), and a source mutated after claim parks the job in
  ``review`` through the replacement pre-check;
- ``interrupted_resume``: a SIGKILLed worker leaves the source untouched; the
  expired lease requeues the clip and a second run completes it;
- ``failed_recover``: an injected pass-2 FFmpeg failure marks the job failed
  with the source byte-identical; after requeue a normal run completes it.

All work happens on staged copies under ``--matrix-root`` (never inside the
archive): with ``--archive-root`` + ``--clip`` the matrix copies chosen clips
out of the mounted archive, or ``--synthetic`` generates test clips. Every
write stays under the matrix root (plus ``--report``), and the emitted report
is sanitized (archive/matrix/scratch prefixes become placeholders).

Run it from the project virtualenv, e.g.::

    .venv/bin/python scripts/rerun_matrix.py run --synthetic \\
        --matrix-root "$MATRIX_ROOT" --report /tmp/rerun-matrix.json

    .venv/bin/python scripts/rerun_matrix.py run \\
        --archive-root "$ARCHIVE_ROOT" \\
        --clip "VIDEOS/MOTORCYCLE/SMALL_CLIP.MP4" \\
        --matrix-root "$MATRIX_ROOT" --report docs/pilot/reports/rerun-matrix.json

The real-folder run is an operator checkpoint: the archive is only ever read,
and the transcript plus the sanitized report are the evidence for issue #13.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from dataclasses import replace
from pathlib import Path, PurePosixPath

from matrix_common import (
    ACTIVE_STATES,
    ROOT_ID,
    MatrixContext,
    ScenarioReport,
    build_report,
    clip_path,
    expire_lease,
    generate_clip,
    mutate_clip,
    preflight_scratch,
    report_replacements,
    requeue,
    run_worker_once,
    seed_queued,
    stage_clip,
    start_worker,
    stop_all_workers,
    stop_worker,
    wait_for_state,
    wait_for_terminal,
    worker_env,
    write_failing_ffmpeg_shim,
    write_report,
)
from pilot_common import (
    SelectionError,
    ToolError,
    assert_outside_root,
    is_safe_relative_path,
    resolve_under_root,
)

from videocompressor.controller.server import ControllerServer, new_token, open_manifest
from videocompressor.controller.store import ControllerStore
from videocompressor.core.config import DEFAULT_TEMPLATE_NAME
from videocompressor.core.fingerprint import fingerprint_file
from videocompressor.core.markers import read_marker
from videocompressor.core.models import (
    Classification,
    EncodeSettings,
    JobState,
    TemplateSettings,
)
from videocompressor.core.replace import find_artifacts, temp_path_for
from videocompressor.core.scan import scan_folder, settings_hash
from videocompressor.core.validate import probe_media_facts, validate_output
from videocompressor.worker.scratch import jobs_root

#: Scenario ids and titles, in the issue's wording order.
SCENARIOS = (
    ("rerun_skip", "completed clips skip after a template change"),
    ("changed_review", "changed source fingerprints go to review"),
    ("interrupted_resume", "interrupted runs resume"),
    ("failed_recover", "failed jobs recover without losing sources"),
)

DEFAULT_SECONDS = 8
DEFAULT_TIMEOUT_S = 600.0
ENCODE_SETTINGS = EncodeSettings(output_height=720)
PRE_REPLACEMENT_STATES = frozenset(
    {JobState.CLAIMED, JobState.COPIED, JobState.ENCODING}
)


def build_parser() -> argparse.ArgumentParser:
    """Build the ``rerun_matrix.py`` argument parser."""
    parser = argparse.ArgumentParser(
        prog="rerun_matrix.py", description="Issue #13 rerun-matrix hardening check."
    )
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    run = subparsers.add_parser("run", help="run the four-scenario rerun matrix")
    run.add_argument(
        "--archive-root", type=Path, default=None, metavar="PATH",
        help="mounted real archive; only ever read (staging source)",
    )
    run.add_argument(
        "--clip", nargs="+", default=[], metavar="REL",
        help="archive-relative clip(s) to stage (one scenario each, cycling)",
    )
    run.add_argument(
        "--synthetic", action="store_true",
        help="generate synthetic test clips instead of staging archive clips",
    )
    run.add_argument(
        "--seconds", type=int, default=DEFAULT_SECONDS,
        help="synthetic clip length (default: %(default)s)",
    )
    run.add_argument(
        "--matrix-root", type=Path, required=True, metavar="PATH",
        help="staging/state root; must be outside the archive and empty",
    )
    run.add_argument(
        "--report", type=Path, default=None, metavar="PATH",
        help="write the sanitized JSON evidence report here",
    )
    run.add_argument("--keep", action="store_true", help="keep the matrix root after a pass")
    run.add_argument(
        "--dry-run", action="store_true", help="print the scenario plan; write nothing"
    )
    run.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT_S,
        help="per-scenario timeout in seconds (default: %(default)s)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the CLI and return the process exit code (0 pass, 1 check, 2 usage)."""
    args = build_parser().parse_args(argv)
    try:
        return cmd_run(args)
    except SelectionError as exc:
        print(f"rerun_matrix: {exc}", file=sys.stderr)
        return 2
    except ToolError as exc:
        print(f"rerun_matrix: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"rerun_matrix: {exc}", file=sys.stderr)
        return 1


def cmd_run(args: argparse.Namespace) -> int:
    """Validate the mode, stage the clips, and run every scenario."""
    synthetic = bool(args.synthetic)
    clips = tuple(args.clip)
    archive_root = args.archive_root.expanduser().resolve() if args.archive_root else None
    matrix_root = args.matrix_root.expanduser().resolve()

    if synthetic and (archive_root is not None or clips):
        raise SelectionError("--synthetic cannot be combined with --archive-root or --clip")
    if not synthetic and (archive_root is None or not clips):
        raise SelectionError("pass --archive-root with at least one --clip, or --synthetic")
    for relative in clips:
        if not is_safe_relative_path(relative):
            raise SelectionError(f"unsafe archive-relative path {relative!r}")
    if archive_root is not None:
        if not archive_root.is_dir():
            raise ToolError(f"archive root {args.archive_root} is not a directory")
        assert_outside_root(matrix_root, archive_root, "--matrix-root")
    if matrix_root.exists() and any(matrix_root.iterdir()):
        raise SelectionError(f"--matrix-root {args.matrix_root} must be new or empty")
    if archive_root is not None:
        for relative in clips:
            if not resolve_under_root(archive_root, relative).is_file():
                raise ToolError(f"clip not found on the archive root: {relative}")

    if args.dry_run:
        print_plan(synthetic, matrix_root, clips, int(args.seconds))
        return 0

    for binary in ("ffmpeg", "ffprobe"):
        if shutil.which(binary) is None:
            raise ToolError(f"{binary} is required on PATH")

    archive_dir = matrix_root / "archive"
    scratch_dir = matrix_root / "scratch"
    state_dir = matrix_root / "state"
    logs_dir = matrix_root / "logs"
    for directory in (archive_dir, scratch_dir, state_dir, logs_dir):
        directory.mkdir(parents=True, exist_ok=True)

    manifest, durable = open_manifest(state_dir / "manifest.sqlite")
    if not durable:
        raise ToolError("the durable SQLite manifest is unavailable")
    store = ControllerStore.open(state_dir / "controller.sqlite")
    token = new_token()
    server = ControllerServer(
        store=store,
        manifest=manifest,
        token=token,
        host="127.0.0.1",
        port=0,
        sweep_interval_seconds=3600.0,  # expiry is driven explicitly by the matrix
    )
    server.start_background()

    sources = clips if clips else ("synthetic/ride.MP4",)
    ctx = MatrixContext(
        archive_dir=archive_dir,
        scratch_dir=scratch_dir,
        state_dir=state_dir,
        logs_dir=logs_dir,
        matrix_root=matrix_root,
        manifest=manifest,
        store=store,
        server=server,
        base_url=f"http://127.0.0.1:{server.port}",
        token=token,
        synthetic=synthetic,
        timeout=float(args.timeout),
        clips=sources,
        staged={
            sid: f"{sid}/{sources[index % len(sources)]}"
            for index, (sid, _) in enumerate(SCENARIOS)
        },
        extra_new=f"rerun_skip/new/{PurePosixPath(sources[0]).name}",
    )

    reports: list[ScenarioReport] = []
    try:
        stage_matrix_clips(ctx, archive_root=archive_root, seconds=int(args.seconds))
        preflight_scratch(ctx)
        for sid, title in SCENARIOS:
            print(f"\n== {sid}: {title}")
            report = ScenarioReport(sid, title)
            started = time.monotonic()
            try:
                SCENARIO_FUNCS[sid](ctx, report)
            except Exception as exc:  # one broken scenario must not abort the matrix
                report.check("scenario ran to completion", False, f"{type(exc).__name__}: {exc}")
            report.seconds = round(time.monotonic() - started, 3)
            reports.append(report)
        document = build_report(
            synthetic=synthetic,
            archive_root=archive_root,
            matrix_root=matrix_root,
            staging=ctx.staging,
            reports=reports,
            manifest=manifest,
        )
        if args.report is not None:
            written = write_report(
                document, args.report.expanduser(), report_replacements(ctx, archive_root)
            )
            print(f"\nreport: {written}")
    finally:
        stop_all_workers(ctx)
        server.stop()
        manifest.close()

    failures = int(document["totals"]["failures"])
    print(f"\n{len(reports)} scenarios, {document['totals']['checks']} checks, {failures} failures")
    ok = all(report.ok for report in reports)
    print("RERUN MATRIX PASSED" if ok else "RERUN MATRIX FAILED")
    if ok and not args.keep:
        shutil.rmtree(matrix_root, ignore_errors=True)
        print(f"matrix root removed: {matrix_root}")
    else:
        print(f"matrix root kept: {matrix_root}")
    return 0 if ok else 1


def print_plan(
    synthetic: bool, matrix_root: Path, clips: tuple[str, ...], seconds: int
) -> None:
    """Print the scenario plan for ``--dry-run`` without touching the filesystem."""
    mode = "synthetic" if synthetic else "archive"
    print(f"rerun matrix ({mode}) dry run")
    print(f"  matrix root: {matrix_root}")
    if synthetic:
        print(f"  synthetic clips: {seconds}s testsrc2/sine seed, copied per scenario")
    else:
        for relative in clips:
            print(f"  archive clip: {relative}")
    for sid, title in SCENARIOS:
        print(f"  {sid}: {title}")
    print("dry run: nothing written")


def stage_matrix_clips(
    ctx: MatrixContext, *, archive_root: Path | None, seconds: int
) -> None:
    """Copy or generate one clip per scenario under the matrix archive."""
    seed_path = ctx.state_dir / "seed.MP4"
    if ctx.synthetic:
        generate_clip(seed_path, seconds)
    destinations = list(ctx.staged.items()) + [("extra", ctx.extra_new)]
    for index, (_, relative) in enumerate(destinations):
        source_relative = ctx.clips[index % len(ctx.clips)]
        if ctx.synthetic:
            source = seed_path
        else:
            assert archive_root is not None  # guarded by the caller
            source = resolve_under_root(archive_root, source_relative)
        if not source.is_file():
            raise ToolError(f"clip not found on the archive root: {source_relative}")
        staged_bytes, elapsed = stage_clip(source, clip_path(ctx, relative))
        ctx.staging.append(
            {
                "relative_path": relative,
                "role": "extra" if relative == ctx.extra_new else "subject",
                "source_bytes": source.stat().st_size,
                "staged_bytes": staged_bytes,
                "stage_seconds": round(elapsed, 3),
                "stage_mb_per_s": round(staged_bytes / 1e6 / elapsed, 2),
            }
        )


def scenario_rerun_skip(ctx: MatrixContext, report: ScenarioReport) -> None:
    """Completed clips skip after a template change; later imports classify new."""
    relative = ctx.staged["rerun_skip"]
    clip = clip_path(ctx, relative)
    source_facts = probe_media_facts(clip)
    source_bytes = clip.stat().st_size
    seed_queued(ctx, relative, clip)

    code, log = run_worker_once(ctx, "matrix-s1", log_name="s1-run")
    record = ctx.manifest.get_clip(ROOT_ID, relative)
    if not report.check(
        "worker completes the clip",
        code == 0 and "settled: complete" in log,
        f"rc={code}",
    ):
        return
    if not report.check(
        "manifest records the clip complete",
        record is not None and record.state is JobState.COMPLETE,
        str(record.state) if record is not None else "no record",
    ):
        return
    assert record is not None
    report.check("output fingerprint matches the file", record.output == fingerprint_file(clip))
    validation = validate_output(clip, ENCODE_SETTINGS, source=source_facts)
    report.check(
        "replacement is HEVC 720p and matches the source",
        validation.ok,
        "; ".join(validation.failures),
    )
    marker = read_marker(clip.parent)
    report.check(
        "folder marker records the completion",
        marker is not None and clip.name in marker.clips,
    )
    report.check("no swap artifacts remain", find_artifacts(clip).all == ())
    output_bytes = clip.stat().st_size
    if not ctx.synthetic:
        report.check(
            "replacement is smaller than the source",
            output_bytes < source_bytes,
            f"{source_bytes} -> {output_bytes} bytes",
        )

    changed = replace(
        TemplateSettings(), daytime_video_bitrate_kbps=1200, evening_video_bitrate_kbps=1000
    )
    report.check(
        "the template change alters the settings hash",
        settings_hash(changed) != settings_hash(TemplateSettings()),
    )
    scan = scan_folder(
        root_id=ROOT_ID,
        root=ctx.archive_dir,
        folder=ctx.archive_dir / "rerun_skip",
        template_id=DEFAULT_TEMPLATE_NAME,
        settings=changed,
        manifest=ctx.manifest,
    )
    rows = {row.relative_path: row for row in scan.rows}
    subject = rows.get(relative)
    report.check(
        "the completed clip still classifies complete",
        subject is not None and subject.classification is Classification.COMPLETE,
        str(subject.classification) if subject is not None else "not scanned",
    )
    later = rows.get(ctx.extra_new)
    report.check(
        "a later import classifies new",
        later is not None and later.classification is Classification.NEW,
        str(later.classification) if later is not None else "not scanned",
    )

    completed = fingerprint_file(clip)
    code, log = run_worker_once(ctx, "matrix-s1-idle", log_name="s1-idle")
    report.check("a rerun worker claims nothing", code == 0 and "settled:" not in log, f"rc={code}")
    report.check(
        "no queued records remain",
        all(record.state is not JobState.QUEUED for record in ctx.manifest.list_clips()),
    )
    report.check(
        "the completed file is untouched by the rerun", fingerprint_file(clip) == completed
    )


def scenario_changed_review(ctx: MatrixContext, report: ScenarioReport) -> None:
    """Changing a completed source flags it changed; a mid-run change parks review."""
    relative = ctx.staged["changed_review"]
    clip = clip_path(ctx, relative)
    seed_queued(ctx, relative, clip)

    code, log = run_worker_once(ctx, "matrix-s2", log_name="s2-run")
    record = ctx.manifest.get_clip(ROOT_ID, relative)
    if not report.check(
        "worker completes the clip",
        code == 0 and record is not None and record.state is JobState.COMPLETE,
        f"rc={code}",
    ):
        return
    completed = fingerprint_file(clip)
    mutated = mutate_clip(clip, "s2a")
    report.check("the mutation changed the file fingerprint", mutated != completed)

    scan = scan_folder(
        root_id=ROOT_ID,
        root=ctx.archive_dir,
        folder=clip.parent,
        template_id=DEFAULT_TEMPLATE_NAME,
        settings=TemplateSettings(),
        manifest=ctx.manifest,
    )
    row = {entry.relative_path: entry for entry in scan.rows}.get(relative)
    report.check(
        "a changed completed clip classifies changed",
        row is not None and row.classification is Classification.CHANGED,
        (row.note or "not scanned") if row is not None else "not scanned",
    )
    code, log = run_worker_once(ctx, "matrix-s2-idle", log_name="s2-idle")
    report.check("a rerun worker claims nothing", code == 0 and "settled:" not in log, f"rc={code}")
    report.check("the changed file was left untouched", fingerprint_file(clip) == mutated)

    requeue(ctx, relative, clip)
    worker = start_worker(ctx, "matrix-s2b", log_name="s2b-run")
    try:
        observed = wait_for_state(ctx, relative, {JobState.ENCODING})
        if not report.check(
            "the worker reaches the encoding stage",
            observed is not None,
            f"observed {observed}" if observed is not None else "not observed; increase --seconds",
        ):
            return
        mutated_mid_run = mutate_clip(clip, "s2b")
        final = wait_for_terminal(ctx, relative)
        report.check(
            "the mid-run source change parks the clip in review",
            final is not None and final.state is JobState.REVIEW,
            str(final.state) if final is not None else "timeout",
        )
        report.check(
            "the review reason names the source change",
            final is not None and "source changed" in (final.error or ""),
            final.error or "",
        )
        report.check(
            "the mutated source is preserved", fingerprint_file(clip) == mutated_mid_run
        )
        report.check("no swap artifacts remain", find_artifacts(clip).all == ())
    finally:
        stop_worker(worker)


def scenario_interrupted_resume(ctx: MatrixContext, report: ScenarioReport) -> None:
    """A SIGKILLed worker leaves the source intact; the retry completes the clip."""
    relative = ctx.staged["interrupted_resume"]
    clip = clip_path(ctx, relative)
    source_facts = probe_media_facts(clip)
    original = fingerprint_file(clip)
    seed_queued(ctx, relative, clip)

    worker = start_worker(ctx, "matrix-s3", log_name="s3-killed")
    try:
        observed = wait_for_state(ctx, relative, ACTIVE_STATES)
        report.check(
            "the worker holds an active (pre-replacement) job",
            observed is not None,
            f"observed {observed}" if observed is not None else "not observed; increase --seconds",
        )
        report.check(
            "the interruption lands before replacement begins",
            observed is not None and observed in PRE_REPLACEMENT_STATES,
            str(observed),
        )
    finally:
        stop_worker(worker, kill=True)
    report.check("SIGKILL left the source byte-identical", fingerprint_file(clip) == original)

    expire_lease(ctx, relative)
    record = ctx.manifest.get_clip(ROOT_ID, relative)
    report.check(
        "the expired lease requeues the clip",
        record is not None and record.state is JobState.QUEUED,
        str(record.state) if record is not None else "no record",
    )
    report.check(
        "the requeue reason is recorded",
        record is not None and "requeued after lease expiry" in (record.error or ""),
        (record.error or "")[:90],
    )
    messages = [event.message for event in ctx.store.list_events(limit=500)]
    report.check(
        "the run log records the lease recovery",
        any(
            message.startswith(f"requeued {relative}") and "lease expired" in message
            for message in messages
        ),
    )

    stale = temp_path_for(clip, "matrix-stale")
    stale.write_bytes(b"stale staged output from an interrupted attempt")
    code, log = run_worker_once(ctx, "matrix-s3-resume", log_name="s3-resume")
    record = ctx.manifest.get_clip(ROOT_ID, relative)
    report.check(
        "the resumed run completes the clip",
        code == 0 and "settled: complete" in log and record is not None
        and record.state is JobState.COMPLETE,
        f"rc={code}",
    )
    report.check("the stale swap temp was reconciled", not stale.exists())
    report.check("no swap artifacts remain", find_artifacts(clip).all == ())
    validation = validate_output(clip, ENCODE_SETTINGS, source=source_facts)
    report.check("the resumed replacement is valid", validation.ok, "; ".join(validation.failures))
    report.check(
        "the source was replaced only by the completed retry",
        fingerprint_file(clip) != original,
    )


def scenario_failed_recover(ctx: MatrixContext, report: ScenarioReport) -> None:
    """An injected pass-2 failure keeps the source; a retry completes it."""
    relative = ctx.staged["failed_recover"]
    clip = clip_path(ctx, relative)
    source_facts = probe_media_facts(clip)
    original = fingerprint_file(clip)
    seed_queued(ctx, relative, clip)

    shim = write_failing_ffmpeg_shim(ctx.state_dir / "shim")
    code, log = run_worker_once(
        ctx,
        "matrix-s4",
        log_name="s4-injected",
        env=worker_env(ctx, extra_path=shim.parent),
    )
    record = ctx.manifest.get_clip(ROOT_ID, relative)
    report.check(
        "the injected pass-2 failure marks the job failed",
        code == 0 and "settled: failed" in log and record is not None
        and record.state is JobState.FAILED,
        f"rc={code}",
    )
    report.check(
        "the failure names the encode error",
        record is not None and "encode failed" in (record.error or ""),
        (record.error or "")[:90],
    )
    report.check(
        "the source is byte-identical after the failure",
        fingerprint_file(clip) == original,
    )
    report.check("no swap artifacts remain", find_artifacts(clip).all == ())
    jobs = jobs_root(ctx.scratch_dir / "matrix-s4")
    report.check("the scratch tree was discarded", jobs.is_dir() and not list(jobs.iterdir()))

    requeue(ctx, relative, clip)
    code, log = run_worker_once(ctx, "matrix-s4-retry", log_name="s4-retry")
    record = ctx.manifest.get_clip(ROOT_ID, relative)
    report.check(
        "the retry completes the clip without losing the source",
        code == 0 and record is not None and record.state is JobState.COMPLETE,
        f"rc={code}",
    )
    validation = validate_output(clip, ENCODE_SETTINGS, source=source_facts)
    report.check("the retried replacement is valid", validation.ok, "; ".join(validation.failures))
    report.check("no swap artifacts remain after the retry", find_artifacts(clip).all == ())


SCENARIO_FUNCS = {
    "rerun_skip": scenario_rerun_skip,
    "changed_review": scenario_changed_review,
    "interrupted_resume": scenario_interrupted_resume,
    "failed_recover": scenario_failed_recover,
}


if __name__ == "__main__":
    raise SystemExit(main())
