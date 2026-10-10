#!/usr/bin/env python3
"""Pilot-run tooling for issue #12: queue seeding, waiting, evidence collection.

Subcommands (run with the project virtualenv):

- ``enqueue``: seed the durable queue (``core.manifest``) with the clips selected
  in ``docs/pilot/pilot-selection.toml``, taking each clip's capture time and
  fingerprint from the issue #11 dry-run reports. The controller must not be
  running while seeding (start it afterwards). ``--dry-run`` writes nothing.
- ``wait``: poll the manifest read-only until every clip is terminal.
- ``collect``: write sanitized evidence (archive-relative paths only) that the
  worker pilot run delivered each selected clip into the pilot area while the
  source files stayed byte-identical.

Sanitization: absolute archive/pilot prefixes are replaced with
``<archive-root>``/``<pilot-root>`` placeholders in everything ``collect``
writes. Never commit the emitted files without checking that rule still holds.

Example::

    .venv/bin/python scripts/pilot_run.py enqueue \\
        --selection docs/pilot/pilot-selection.toml \\
        --root "$ARCHIVE_ROOT" --root-id camera \\
        --manifest "$PILOT_ROOT/pilot-manifest.sqlite"
    .venv/bin/python scripts/pilot_run.py wait --manifest "$PILOT_ROOT/pilot-manifest.sqlite"
    .venv/bin/python scripts/pilot_run.py collect \\
        --manifest "$PILOT_ROOT/pilot-manifest.sqlite" \\
        --root "$ARCHIVE_ROOT" --pilot-root "$PILOT_ROOT" \\
        --out docs/pilot/reports/pilot-run.json
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pilot_common import (
    ARCHIVE_PLACEHOLDER,
    PILOT_PLACEHOLDER,
    SelectionClip,
    SelectionError,
    ToolError,
    assert_outside_root,
    is_safe_relative_path,
    load_selection,
    resolve_under_root,
    sanitize_text,
)

from videocompressor.core.config import DEFAULT_TEMPLATE_NAME
from videocompressor.core.fingerprint import fingerprint_confirms, fingerprint_file
from videocompressor.core.manifest import Manifest
from videocompressor.core.models import (
    CaptureTime,
    CaptureTimeMode,
    Classification,
    ClipRecord,
    EncodeSettings,
    Fingerprint,
    JobState,
)
from videocompressor.core.probe import FFprobeUnavailableError, ProbeError, probe_media
from videocompressor.core.scan import derive_root_id
from videocompressor.core.validate import facts_from_media_info, validate_output

DEFAULT_REPORTS = Path("docs/pilot/reports")
DEFAULT_SELECTION = Path("docs/pilot/pilot-selection.toml")

#: States the controller can still act on; `wait` exits when none remain.
LIVE_STATES = frozenset(
    {
        JobState.QUEUED,
        JobState.CLAIMED,
        JobState.COPIED,
        JobState.ENCODING,
        JobState.VALIDATED,
        JobState.TRANSFERRING,
        JobState.REPLACING,
    }
)

#: Existing records these states may be re-seeded without `--requeue`.
_REQUEUEABLE = frozenset({JobState.QUEUED, JobState.REVIEW})


def build_parser() -> argparse.ArgumentParser:
    """Build the ``pilot_run.py`` argument parser."""
    parser = argparse.ArgumentParser(
        prog="pilot_run.py", description="Issue #12 pilot-run tooling (queue, wait, evidence)."
    )
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    enqueue = subparsers.add_parser("enqueue", help="seed pilot clips into the durable queue")
    enqueue.add_argument("--selection", type=Path, default=DEFAULT_SELECTION)
    enqueue.add_argument("--root", type=Path, required=True, help="local archive root directory")
    enqueue.add_argument("--manifest", type=Path, required=True, help="manifest database to seed")
    enqueue.add_argument("--root-id", default=None, help="manifest root id (default: derived)")
    enqueue.add_argument(
        "--reports",
        type=Path,
        nargs="*",
        default=None,
        help=f"issue #11 scan reports (default: {DEFAULT_REPORTS})",
    )
    enqueue.add_argument("--clip", default=None, help="seed only this archive-relative clip")
    enqueue.add_argument(
        "--allow-changed", action="store_true", help="warn instead of failing on changed clips"
    )
    enqueue.add_argument(
        "--requeue", action="store_true", help="allow resetting an already-processed record"
    )
    enqueue.add_argument("--dry-run", action="store_true", help="validate and print, write nothing")
    enqueue.add_argument("--json", action="store_true", help="print machine-readable output")

    wait = subparsers.add_parser("wait", help="wait until every manifest clip is terminal")
    wait.add_argument("--manifest", type=Path, required=True)
    wait.add_argument("--timeout", type=float, default=7200.0, help="seconds (default: 7200)")
    wait.add_argument("--poll", type=float, default=5.0, help="seconds between polls")
    wait.add_argument("--json", action="store_true", help="print only the final summary as JSON")

    collect = subparsers.add_parser("collect", help="write sanitized pilot-run evidence")
    collect.add_argument("--manifest", type=Path, required=True)
    collect.add_argument("--root", type=Path, required=True)
    collect.add_argument("--pilot-root", type=Path, required=True)
    collect.add_argument("--out", type=Path, required=True)
    collect.add_argument("--selection", type=Path, default=DEFAULT_SELECTION)
    collect.add_argument("--clip", default=None, help="collect only this archive-relative clip")
    collect.add_argument("--no-probe", action="store_true", help="skip ffprobe/validation facts")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the CLI and return the process exit code."""
    args = build_parser().parse_args(argv)
    try:
        if args.command == "enqueue":
            return cmd_enqueue(args)
        if args.command == "wait":
            return cmd_wait(args)
        return cmd_collect(args)
    except SelectionError as exc:
        print(f"pilot_run: {exc}", file=sys.stderr)
        return 2
    except ToolError as exc:
        print(f"pilot_run: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"pilot_run: {exc}", file=sys.stderr)
        return 1


# -- enqueue ------------------------------------------------------------------


def cmd_enqueue(args: argparse.Namespace) -> int:
    """Seed selected clips as queued manifest records, in selection order."""
    selection = load_selection(args.selection)
    clips = selected_clips(selection, args.clip)
    if not clips:
        raise ToolError(f"selection has no clip matching {args.clip!r}")
    root = args.root.expanduser()
    if not root.is_dir():
        raise SelectionError(f"archive root {args.root} is not a directory")
    manifest_path = args.manifest.expanduser()
    assert_outside_root(manifest_path, root, "--manifest")
    root_id = args.root_id or derive_root_id(root)
    facts = load_report_facts(args.reports)

    existing: dict[str, JobState] = {}
    if manifest_path.is_file():
        with Manifest.open(manifest_path, create=False) as manifest:
            for clip in clips:
                record = manifest.get_clip(root_id, clip.relative_path)
                if record is not None:
                    existing[clip.relative_path] = record.state

    base = datetime.now(UTC)
    planned: list[tuple[SelectionClip, ClipRecord]] = []
    for index, clip in enumerate(clips):
        source = resolve_under_root(root, clip.relative_path)
        if not source.is_file():
            raise ToolError(f"clip not found on disk: {clip.relative_path}")
        fresh = fingerprint_file(source)
        when = report_capture_time(clip, facts, fresh, allow_changed=args.allow_changed)
        state = existing.get(clip.relative_path)
        if state is not None and state not in _REQUEUEABLE and not args.requeue:
            raise ToolError(
                f"{clip.relative_path} is {state} in the manifest; pass --requeue to reset it"
            )
        stamp = base + timedelta(milliseconds=index)
        planned.append((clip, clip_record(root_id, clip, fresh, when, stamp)))

    if args.dry_run:
        if args.json:
            print(json.dumps(_plan_document(root_id, planned), indent=2, sort_keys=True))
        else:
            for clip, record in planned:
                when = record.capture_time.when if record.capture_time is not None else None
                print(f"{clip.relative_path}: {clip.category}/{clip.bucket} capture {when}")
            print(f"pilot_run: dry run, would queue {len(planned)} clip(s)")
        return 0

    with Manifest.open(manifest_path, create=True) as manifest:
        for _, record in planned:
            manifest.upsert_clip(record)
    if args.json:
        print(json.dumps(_plan_document(root_id, planned), indent=2, sort_keys=True))
    else:
        print(f"pilot_run: queued {len(planned)} clip(s) under root {root_id}")
    return 0


def selected_clips(
    selection: tuple[SelectionClip, ...], clip: str | None
) -> tuple[SelectionClip, ...]:
    """Filter the selection, or synthesize a replacement-only entry for ``--clip``."""
    if clip is None:
        return selection
    matches = tuple(entry for entry in selection if entry.relative_path == clip)
    if matches:
        return matches
    if not is_safe_relative_path(clip):
        raise SelectionError(f"unsafe archive-relative path {clip!r}")
    return (
        SelectionClip(
            relative_path=clip, category="replacement", bucket="", candidates=(), shots=()
        ),
    )


def clip_record(
    root_id: str, clip: SelectionClip, source: Fingerprint, when: datetime, stamp: datetime
) -> ClipRecord:
    """Build the queued manifest record for one selected clip."""
    return ClipRecord(
        root_id=root_id,
        relative_path=clip.relative_path,
        source=source,
        state=JobState.QUEUED,
        classification=Classification.NEW,
        capture_time=CaptureTime(when=when, mode=CaptureTimeMode.LOCAL_WALL_CLOCK),
        template_id=DEFAULT_TEMPLATE_NAME,
        created_at=stamp,
        updated_at=stamp,
    )


def report_capture_time(
    clip: SelectionClip,
    facts: dict[str, dict[str, Any]],
    fresh: Fingerprint,
    *,
    allow_changed: bool,
) -> datetime:
    """Cross-check a clip against its dry-run report and return the capture time."""
    report = facts.get(clip.relative_path)
    if report is None:
        raise ToolError(
            f"no dry-run report entry for {clip.relative_path}; "
            "pass --reports with the issue #11 scan report"
        )
    size = report.get("size_bytes")
    digest = report.get("digest")
    if isinstance(size, bool) or not isinstance(size, int):
        raise ToolError(f"report entry for {clip.relative_path} is missing size_bytes/digest")
    if not isinstance(digest, str) or not digest:
        raise ToolError(f"report entry for {clip.relative_path} is missing size_bytes/digest")
    if (fresh.size_bytes, fresh.digest) != (size, digest):
        message = (
            f"{clip.relative_path} changed since the dry run "
            f"({fresh.size_bytes} B/{fresh.digest[:12]} vs report {size} B/{digest[:12]})"
        )
        if not allow_changed:
            raise ToolError(f"{message}; re-run vc scan or pass --allow-changed")
        print(f"pilot_run: warning: {message}", file=sys.stderr)
    bucket = report.get("bucket")
    if clip.bucket and isinstance(bucket, str) and bucket != clip.bucket:
        raise ToolError(
            f"{clip.relative_path}: selection bucket {clip.bucket!r} conflicts "
            f"with the report bucket {bucket!r}"
        )
    capture = report.get("capture_time")
    if not isinstance(capture, str) or not capture:
        raise ToolError(f"report entry for {clip.relative_path} has no capture_time")
    try:
        when = datetime.fromisoformat(capture)
    except ValueError as exc:
        raise ToolError(f"report capture_time {capture!r} is not ISO-8601") from exc
    if when.tzinfo is None:
        raise ToolError(f"report capture_time {capture!r} has no UTC offset")
    return when


def load_report_facts(paths: list[Path] | None) -> dict[str, dict[str, Any]]:
    """Index ``relative_path -> report clip row`` over the #11 dry-run reports."""
    facts: dict[str, dict[str, Any]] = {}
    for raw in paths if paths else [DEFAULT_REPORTS]:
        path = raw.expanduser()
        if path.is_dir():
            for candidate in sorted(path.glob("*.json")):
                data = _read_json(candidate, required=False)
                if data is not None:
                    _merge_facts(facts, data)
        elif path.is_file():
            data = _read_json(path, required=True)
            if data is not None:
                _merge_facts(facts, data)
        else:
            raise ToolError(f"scan report {raw} does not exist")
    return facts


def _read_json(path: Path, *, required: bool) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        if required:
            raise ToolError(f"cannot read scan report {path}: {exc}") from exc
        return None
    if not isinstance(data, dict):
        if required:
            raise ToolError(f"scan report {path} is not a JSON object")
        return None
    return data


def _merge_facts(facts: dict[str, dict[str, Any]], data: dict[str, Any]) -> None:
    rows = data.get("clips")
    if not isinstance(rows, list):
        return
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("relative_path"), str):
            facts.setdefault(row["relative_path"], row)


def _plan_document(root_id: str, planned: list[tuple[SelectionClip, ClipRecord]]) -> dict[str, Any]:
    return {
        "root_id": root_id,
        "clips": [
            {
                "relative_path": clip.relative_path,
                "category": clip.category,
                "bucket": clip.bucket,
                "capture_time": record.capture_time.when.isoformat()
                if record.capture_time is not None and record.capture_time.when is not None
                else None,
            }
            for clip, record in planned
        ],
        "queued": len(planned),
    }


# -- wait ---------------------------------------------------------------------


def cmd_wait(args: argparse.Namespace) -> int:
    """Poll a manifest read-only until no clip is in a live state."""
    path = args.manifest.expanduser()
    if not path.is_file():
        raise SelectionError(f"manifest {args.manifest} does not exist")
    manifest = Manifest.open(path, create=False)
    deadline = time.monotonic() + args.timeout
    previous: dict[str, str] = {}
    last_counts: tuple[int, int] | None = None
    try:
        while True:
            records = None
            try:
                records = manifest.list_clips()
            except sqlite3.OperationalError as exc:
                print(f"pilot_run: manifest busy ({exc}); retrying", file=sys.stderr)
            if records is not None:
                current = {record.relative_path: str(record.state) for record in records}
                if not args.json:
                    for relative_path, state in current.items():
                        if previous.get(relative_path) != state:
                            print(f"{relative_path}: {state}")
                previous = current
                live = [rel for rel, state in current.items() if JobState(state) in LIVE_STATES]
                counts = (len(current) - len(live), len(current))
                if counts != last_counts and not args.json:
                    print(f"pilot_run: {counts[0]}/{counts[1]} clip(s) terminal")
                    last_counts = counts
                if not live:
                    if args.json:
                        print(
                            json.dumps(
                                {"total": len(current), "clips": current},
                                indent=2,
                                sort_keys=True,
                            )
                        )
                    return 0
            if time.monotonic() >= deadline:
                print(f"pilot_run: timed out after {args.timeout:g}s", file=sys.stderr)
                return 2
            time.sleep(args.poll)
    finally:
        manifest.close()


# -- collect ------------------------------------------------------------------


def cmd_collect(args: argparse.Namespace) -> int:
    """Write sanitized pilot-run evidence for the manifest's clips."""
    path = args.manifest.expanduser()
    if not path.is_file():
        raise SelectionError(f"manifest {args.manifest} does not exist")
    root = args.root.expanduser()
    if not root.is_dir():
        raise SelectionError(f"archive root {args.root} is not a directory")
    pilot_root = args.pilot_root.expanduser()
    categories = selection_categories(args.selection)
    probe_enabled = not args.no_probe and shutil.which("ffprobe") is not None

    with Manifest.open(path, create=False) as manifest:
        records = [
            record
            for record in manifest.list_clips()
            if args.clip is None or record.relative_path == args.clip
        ]
    if args.clip is not None and not records:
        raise ToolError(f"no manifest record for {args.clip}")

    clips = [
        collect_record(record, root, pilot_root, categories, probe_enabled) for record in records
    ]
    document = {"generated_at": datetime.now(UTC).isoformat(), "clips": clips}
    replacements = {
        str(root.resolve()): ARCHIVE_PLACEHOLDER,
        str(pilot_root.resolve()): PILOT_PLACEHOLDER,
    }
    text = sanitize_text(json.dumps(document, indent=2, sort_keys=True), replacements)
    out = args.out.expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text + "\n", encoding="utf-8")

    changed = [entry["relative_path"] for entry in clips if not entry["source_unchanged"]]
    if changed:
        print(
            f"pilot_run: warning: {len(changed)} source(s) do not match the seeded "
            f"fingerprint: {', '.join(changed)}",
            file=sys.stderr,
        )
    print(f"pilot_run: wrote {len(clips)} record(s) to {out}")
    return 0


def collect_record(
    record: ClipRecord,
    root: Path,
    pilot_root: Path,
    categories: dict[str, str],
    probe_enabled: bool,
) -> dict[str, Any]:
    """Build one sanitized evidence entry (absolute paths are stripped later)."""
    entry: dict[str, Any] = {
        "relative_path": record.relative_path,
        "state": str(record.state),
        "detail": record.error or "",
        "source_unchanged": False,
        "source_bytes": None,
        "output_exists": False,
        "output_bytes": None,
    }
    if record.relative_path in categories:
        entry["category"] = categories[record.relative_path]
    source = resolve_under_root(root, record.relative_path)
    if source.is_file():
        fresh = fingerprint_file(source)
        entry["source_bytes"] = fresh.size_bytes
        entry["source_unchanged"] = fingerprint_confirms(record.source, fresh)
    output = resolve_under_root(pilot_root, record.relative_path)
    if output.is_file():
        entry["output_exists"] = True
        entry["output_bytes"] = output.stat().st_size
    if probe_enabled and output.is_file():
        try:
            output_facts = facts_from_media_info(probe_media(output))
            entry["media"] = {
                "video_codec": output_facts.video_codec,
                "height": output_facts.height,
                "duration_s": output_facts.duration_s,
                "has_audio": output_facts.has_audio,
            }
            if source.is_file():
                source_facts = facts_from_media_info(probe_media(source))
                result = validate_output(
                    output, EncodeSettings(output_height=720), source=source_facts
                )
                entry["validation"] = {"ok": result.ok, "failures": list(result.failures)}
        except (ProbeError, FFprobeUnavailableError, ValueError, OSError) as exc:
            entry["probe_error"] = str(exc)
    return entry


def selection_categories(path: Path) -> dict[str, str]:
    """``relative_path -> category`` from the selection, tolerating a missing file."""
    try:
        return {clip.relative_path: clip.category for clip in load_selection(path)}
    except SelectionError as exc:
        print(f"pilot_run: warning: {exc}", file=sys.stderr)
        return {}


if __name__ == "__main__":
    raise SystemExit(main())
