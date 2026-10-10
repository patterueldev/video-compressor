"""``vc probe``: probe one clip and report capture time, mode, bucket, and review (issue #2)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from videocompressor.cli.constants import EXIT_ERROR
from videocompressor.core.capture_time import bucket_for, resolve_capture_time
from videocompressor.core.config import DEFAULT_TEMPLATE_NAME, load_config
from videocompressor.core.models import (
    Bucket,
    CaptureIssue,
    CaptureTime,
    CaptureTimeMode,
    TemplateSettings,
)
from videocompressor.core.probe import FFprobeUnavailableError, ProbeError, probe_media

NAME = "probe"
HELP = "probe one clip and report capture time and bucket (issue #2)"


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(NAME, help=HELP)
    parser.add_argument("file", type=Path, metavar="FILE", help="clip to probe")


def run(args: argparse.Namespace) -> int:
    """Probe one clip and print its capture time, mode, bucket, and review reason.

    Read-only: probing never writes to the archive. A review reason is data in the
    report (exit code 0); only environment or configuration failures exit 1.
    """
    path: Path = args.file
    try:
        config = load_config()
    except (OSError, ValueError) as exc:
        print(f"vc probe: cannot load config: {exc}", file=sys.stderr)
        return EXIT_ERROR

    mode = config.capture_time.mode
    template = config.templates.get(DEFAULT_TEMPLATE_NAME, TemplateSettings())

    try:
        media = probe_media(path)
    except FFprobeUnavailableError as exc:
        print(f"vc probe: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except ProbeError as exc:
        _print_probe_report(path, mode=mode, capture=None, bucket=None, review=exc.kind)
        return 0

    try:
        capture = resolve_capture_time(media, mode=mode, timezone=template.timezone)
        bucket = (
            bucket_for(
                capture.when,
                evening_start=template.evening_start,
                timezone=template.timezone,
            )
            if capture.when is not None
            else None
        )
    except ValueError as exc:
        print(f"vc probe: {exc}", file=sys.stderr)
        return EXIT_ERROR

    _print_probe_report(path, mode=mode, capture=capture, bucket=bucket, review=capture.issue)
    return 0


def _print_probe_report(
    path: Path,
    *,
    mode: CaptureTimeMode,
    capture: CaptureTime | None,
    bucket: Bucket | None,
    review: CaptureIssue | None,
) -> None:
    lines = [
        f"file: {path}",
        f"capture_time: {_when_text(capture)}",
        f"raw: {capture.raw if capture and capture.raw else 'none'}",
        f"source: {capture.source if capture and capture.source else 'none'}",
        f"mode: {mode.value}",
        f"bucket: {bucket.value if bucket else 'none'}",
        f"review: {review.value if review else 'none'}",
    ]
    print("\n".join(lines))


def _when_text(capture: CaptureTime | None) -> str:
    if capture is None or capture.when is None:
        return "none"
    return capture.when.isoformat()
