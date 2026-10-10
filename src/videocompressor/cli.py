"""Command-line entry points for Video Compressor.

``vc probe`` belongs to issue #2; every other subcommand is owned by a later
issue and exits with a clear "not implemented" message (exit code 2) instead of
doing nothing.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from videocompressor import __version__
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

EXIT_ERROR = 1
EXIT_NOT_IMPLEMENTED = 2

#: Subcommands stubbed here; the owning issue implements each one.
_SUBCOMMANDS = {
    "scan": "scan an archive root and report a dry run (issue #3)",
    "worker": "run the worker agent for this node (issue #6)",
    "controller": "run the local controller and worker registry (issue #7)",
    "gui": "launch the graphical interface (issue #8)",
}


def build_parser() -> argparse.ArgumentParser:
    """Build the ``vc`` argument parser."""
    parser = argparse.ArgumentParser(
        prog="vc",
        description="GUI-driven, resumable GoPro video compressor.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    probe_parser = subparsers.add_parser(
        "probe", help="probe one clip and report capture time and bucket (issue #2)"
    )
    probe_parser.add_argument("file", type=Path, metavar="FILE", help="clip to probe")
    for name, help_text in _SUBCOMMANDS.items():
        subparsers.add_parser(name, help=help_text)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return the process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_usage(sys.stderr)
        print("vc: choose a command (see 'vc --help')", file=sys.stderr)
        return EXIT_NOT_IMPLEMENTED

    if args.command == "probe":
        return _run_probe(args.file)

    print(f"vc {args.command}: not implemented yet", file=sys.stderr)
    return EXIT_NOT_IMPLEMENTED


def _run_probe(path: Path) -> int:
    """Probe one clip and print its capture time, mode, bucket, and review reason.

    Read-only: probing never writes to the archive. A review reason is data in the
    report (exit code 0); only environment or configuration failures exit 1.
    """
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


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
