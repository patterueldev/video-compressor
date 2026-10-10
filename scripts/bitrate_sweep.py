#!/usr/bin/env python3
"""Candidate-bitrate sweep and sign/plate stills for the issue #12 settings review.

For every selected clip and every candidate bitrate in
``docs/pilot/pilot-selection.toml`` this tool:

- copies the clip (or an optional ``segment``) into local scratch and verifies
  the copy,
- runs the same two-pass x265 recipe as the worker (720p, template preset),
- validates the output with ``core.validate.validate_output``,
- extracts the sign/plate crops listed as ``shots`` from every candidate output
  plus a source reference still (``scale=-2:720`` then the recorded crop), and
- writes a sanitized sweep report (archive-relative paths only; absolute
  prefixes replaced with placeholders).

Sources are only ever read. Output video files stay in a pilot area,
``--review-dir`` receives the stills, ``--report`` the JSON. ``--dry-run``
prints the matrix and writes nothing.

Example::

    .venv/bin/python scripts/bitrate_sweep.py run \\
        --selection docs/pilot/pilot-selection.toml \\
        --root "$ARCHIVE_ROOT" \\
        --videos-dir "$PILOT_ROOT/_sweep" \\
        --review-dir docs/pilot/review \\
        --report docs/pilot/reports/sweep.json
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pilot_common import (
    ARCHIVE_PLACEHOLDER,
    SCRATCH_PLACEHOLDER,
    SelectionClip,
    SelectionError,
    ToolError,
    assert_outside_root,
    load_selection,
    resolve_under_root,
    sanitize_text,
    scaled_width_for_height,
    shot_label,
    slug,
    validate_crop,
)

from videocompressor.core.ffmpeg import (
    EncodeError,
    ScratchSpaceError,
    check_scratch_space,
    estimate_output_bytes,
    run_two_pass_encode,
)
from videocompressor.core.fingerprint import fingerprint_confirms, fingerprint_file
from videocompressor.core.models import EncodeSettings
from videocompressor.core.probe import probe_media
from videocompressor.core.validate import facts_from_media_info, validate_output

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"

DEFAULT_OUTPUT_HEIGHT = 720
DEFAULT_AUDIO_KBPS = 96
DEFAULT_PRESET = "medium"


def build_parser() -> argparse.ArgumentParser:
    """Build the ``bitrate_sweep.py`` argument parser."""
    parser = argparse.ArgumentParser(
        prog="bitrate_sweep.py", description="Issue #12 candidate-bitrate sweep."
    )
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    run = subparsers.add_parser("run", help="encode candidates and extract comparison stills")
    run.add_argument("--selection", type=Path, required=True)
    run.add_argument("--root", type=Path, required=True, help="local archive root directory")
    run.add_argument("--videos-dir", type=Path, required=True, help="pilot area for sweep videos")
    run.add_argument("--review-dir", type=Path, required=True, help="stills output directory")
    run.add_argument("--report", type=Path, required=True, help="sanitized sweep report JSON")
    run.add_argument("--scratch", type=Path, default=None, help="scratch dir (default: temp dir)")
    run.add_argument("--only", default=None, help="restrict to clips whose path contains this")
    run.add_argument("--dry-run", action="store_true", help="print the matrix, write nothing")
    run.add_argument("--keep-scratch", action="store_true", help="keep an auto-created scratch dir")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the CLI and return the process exit code."""
    args = build_parser().parse_args(argv)
    try:
        return cmd_run(args)
    except SelectionError as exc:
        print(f"bitrate_sweep: {exc}", file=sys.stderr)
        return 2
    except ToolError as exc:
        print(f"bitrate_sweep: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"bitrate_sweep: {exc}", file=sys.stderr)
        return 1


def cmd_run(args: argparse.Namespace) -> int:
    """Run the sweep and write the sanitized report."""
    selection = load_selection(args.selection)
    clips = tuple(
        clip for clip in selection if args.only is None or args.only in clip.relative_path
    )
    if not clips:
        raise ToolError(f"selection has no clip matching {args.only!r}")
    root = args.root.expanduser()
    if not root.is_dir():
        raise SelectionError(f"archive root {args.root} is not a directory")
    videos_dir = args.videos_dir.expanduser()
    review_dir = args.review_dir.expanduser()
    report_path = args.report.expanduser()
    assert_outside_root(videos_dir, root, "--videos-dir")
    assert_outside_root(review_dir, root, "--review-dir")
    assert_outside_root(report_path, root, "--report")
    if args.scratch is not None:
        assert_outside_root(args.scratch.expanduser(), root, "--scratch")

    if args.dry_run:
        print_plan(clips, videos_dir, review_dir)
        return 0
    if shutil.which(FFMPEG) is None or shutil.which(FFPROBE) is None:
        raise ToolError("ffmpeg and ffprobe must be on PATH")

    own_scratch = args.scratch is None
    scratch = args.scratch.expanduser() if args.scratch is not None else Path(
        tempfile.mkdtemp(prefix="vc-sweep-")
    )
    scratch.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    failures = 0
    try:
        for clip in clips:
            entry, clip_failures = sweep_clip(clip, root, videos_dir, review_dir, scratch)
            results.append(entry)
            failures += clip_failures
    finally:
        if own_scratch and not args.keep_scratch:
            shutil.rmtree(scratch, ignore_errors=True)

    document = {
        "generated_at": datetime.now(UTC).isoformat(),
        "output_height": DEFAULT_OUTPUT_HEIGHT,
        "audio_bitrate_kbps": DEFAULT_AUDIO_KBPS,
        "ffmpeg_preset": DEFAULT_PRESET,
        "clips": results,
    }
    replacements = {
        str(root.resolve()): ARCHIVE_PLACEHOLDER,
        str(scratch.resolve()): SCRATCH_PLACEHOLDER,
        str(videos_dir.resolve()): "<videos-dir>",
        str(review_dir.resolve()): "<review-dir>",
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        sanitize_text(json.dumps(document, indent=2, sort_keys=True), replacements) + "\n",
        encoding="utf-8",
    )
    encodes = sum(len(clip.candidates) for clip in clips)
    print(f"bitrate_sweep: {encodes} encode(s), {failures} failure(s); report {report_path}")
    return 1 if failures else 0


def sweep_clip(
    clip: SelectionClip, root: Path, videos_dir: Path, review_dir: Path, scratch: Path
) -> tuple[dict[str, Any], int]:
    """Encode every candidate for one clip, extract stills, return (entry, failures)."""
    source = resolve_under_root(root, clip.relative_path)
    if not source.is_file():
        raise ToolError(f"clip not found on disk: {clip.relative_path}")
    clip_slug = slug(clip.relative_path)
    clip_scratch = scratch / clip_slug
    clip_scratch.mkdir(parents=True, exist_ok=True)
    input_path = clip_scratch / f"input{source.suffix.lower() or '.mp4'}"
    if clip.segment is not None:
        extract_segment(source, input_path, clip.segment)
    else:
        shutil.copyfile(source, input_path)
        if not fingerprint_confirms(fingerprint_file(source), fingerprint_file(input_path)):
            raise ToolError(f"scratch copy of {clip.relative_path} does not verify")

    facts = facts_from_media_info(probe_media(input_path))
    if facts.duration_s is None or not facts.width or not facts.height:
        raise ToolError(f"cannot read duration/dimensions for {clip.relative_path}")
    scaled_width = scaled_width_for_height(
        facts.width, facts.height, output_height=DEFAULT_OUTPUT_HEIGHT
    )
    for shot in clip.shots:
        validate_crop(shot, scaled_width, DEFAULT_OUTPUT_HEIGHT)

    still_root = review_dir / clip_slug
    shots = [shot_entry(shot) for shot in clip.shots]
    failures = 0
    for index, shot in enumerate(clip.shots):
        destination = still_root / (
            f"source__t{timestamp_label(shot.t_s)}__{shot_label(shot, index)}.png"
        )
        try:
            extract_still(input_path, shot, destination)
            shots[index]["stills"]["source"] = destination.relative_to(review_dir).as_posix()
        except (subprocess.CalledProcessError, OSError) as exc:
            shots[index]["extraction_errors"].append(f"source: {_short(exc)}")
            failures += 1

    candidates: list[dict[str, Any]] = []
    for kbps in clip.candidates:
        entry: dict[str, Any] = {"kbps": kbps}
        settings = EncodeSettings(
            output_height=DEFAULT_OUTPUT_HEIGHT,
            video_bitrate_kbps=kbps,
            audio_bitrate_kbps=DEFAULT_AUDIO_KBPS,
            ffmpeg_preset=DEFAULT_PRESET,
        )
        output = videos_dir / clip_slug / f"{kbps}kbps.mp4"
        entry["video"] = output.relative_to(videos_dir).as_posix()
        try:
            check_scratch_space(
                scratch,
                source_bytes=input_path.stat().st_size,
                estimated_output_bytes=(
                    estimate_output_bytes(facts.duration_s, kbps, DEFAULT_AUDIO_KBPS) or 0
                ),
            )
            result = run_two_pass_encode(
                input_path,
                output,
                settings,
                duration_s=facts.duration_s,
                passlog_prefix=clip_scratch / f"passlog-{kbps}",
            )
        except ScratchSpaceError as exc:
            entry["error"] = f"scratch space: {exc}"
            candidates.append(entry)
            failures += 1
            continue
        except EncodeError as exc:
            entry["error"] = f"encode failed: {exc}"
            candidates.append(entry)
            failures += 1
            continue
        entry["output_bytes"] = result.output_bytes
        entry["pass_seconds"] = list(result.pass_seconds)
        validation = validate_output(output, settings, source=facts)
        entry["validation"] = {"ok": validation.ok, "failures": list(validation.failures)}
        if not validation.ok:
            failures += 1
        for index, shot in enumerate(clip.shots):
            destination = still_root / (
                f"{kbps}kbps__t{timestamp_label(shot.t_s)}__{shot_label(shot, index)}.png"
            )
            try:
                extract_still(output, shot, destination)
                shots[index]["stills"][str(kbps)] = destination.relative_to(review_dir).as_posix()
            except (subprocess.CalledProcessError, OSError) as exc:
                shots[index]["extraction_errors"].append(f"{kbps}: {_short(exc)}")
                failures += 1
        candidates.append(entry)

    entry_clip = {
        "relative_path": clip.relative_path,
        "category": clip.category,
        "bucket": clip.bucket,
        "duration_s": facts.duration_s,
        "source_bytes": source.stat().st_size,
        "segment": None
        if clip.segment is None
        else {"start_s": clip.segment.start_s, "duration_s": clip.segment.duration_s},
        "shots": shots,
        "candidates": candidates,
    }
    return entry_clip, failures


def shot_entry(shot: Any) -> dict[str, Any]:
    """One sanitized shot record with its still file names."""
    return {
        "t_s": shot.t_s,
        "crop": {"w": shot.crop_w, "h": shot.crop_h, "x": shot.crop_x, "y": shot.crop_y},
        "note": shot.note,
        "stills": {},
        "extraction_errors": [],
    }


def timestamp_label(t_s: float) -> str:
    """Short file-name timestamp: ``12`` or ``12.5``."""
    return f"{t_s:g}"


def extract_segment(source: Path, destination: Path, segment: Any) -> None:
    """Stream-copy one trim of the source into scratch."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [
        FFMPEG,
        "-y",
        "-v",
        "error",
        "-ss",
        f"{segment.start_s:.3f}",
        "-i",
        str(source),
        "-t",
        f"{segment.duration_s:.3f}",
        "-c",
        "copy",
        str(destination),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        raise ToolError(f"segment extraction failed: {_short(exc)}") from exc


def extract_still(input_path: Path, shot: Any, destination: Path) -> None:
    """Extract one 720p crop PNG at the shot's timestamp."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [
        FFMPEG,
        "-y",
        "-v",
        "error",
        "-ss",
        f"{shot.t_s:.3f}",
        "-i",
        str(input_path),
        "-frames:v",
        "1",
        "-vf",
        (
            f"scale=-2:{DEFAULT_OUTPUT_HEIGHT},"
            f"crop={shot.crop_w}:{shot.crop_h}:{shot.crop_x}:{shot.crop_y}"
        ),
        str(destination),
    ]
    subprocess.run(command, check=True, capture_output=True, text=True)


def print_plan(clips: tuple[SelectionClip, ...], videos_dir: Path, review_dir: Path) -> None:
    """Print the sweep matrix without touching the filesystem."""
    encodes = 0
    for clip in clips:
        encodes += len(clip.candidates)
        segment = (
            "full"
            if clip.segment is None
            else f"segment {clip.segment.start_s:g}s+{clip.segment.duration_s:g}s"
        )
        print(
            f"{clip.relative_path} [{clip.category}] candidates={list(clip.candidates)} "
            f"{segment} shots={len(clip.shots)}"
        )
    print(f"bitrate_sweep: {len(clips)} clip(s), {encodes} encode(s)")
    print(f"bitrate_sweep: videos -> {videos_dir}, stills -> {review_dir}")
    print("bitrate_sweep: dry run; nothing written")


def _short(exc: BaseException) -> str:
    """One-line exception detail (ffmpeg stderr tail when available)."""
    stderr = getattr(exc, "stderr", None)
    if isinstance(stderr, str) and stderr.strip():
        return stderr.strip().splitlines()[-1]
    return str(exc)


if __name__ == "__main__":
    raise SystemExit(main())
