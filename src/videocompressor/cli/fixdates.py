"""``vc fix-dates``: stamp output creation dates from capture times (issue #86).

The sweep reads each video's embedded ``creation_time`` and writes it back as
the filesystem creation date, so a mixed media folder sorts by capture date.
The interpretation follows the rest of the app: the configured capture mode
and the default template's timezone. ``--dry-run`` reports without writing.
Per-file failures are listed but never fail the command; an unusable folder or
a platform that cannot set birth times exits non-zero.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from videocompressor.cli.constants import EXIT_ERROR, EXIT_USAGE
from videocompressor.core.config import load_config
from videocompressor.core.date_stamp import (
    StampReport,
    StampStatus,
    supported,
    sweep_folder,
    timezone_for_config,
)

NAME = "fix-dates"
HELP = "stamp a folder's creation dates from embedded capture times (issue #86)"

#: Failing paths listed after the summary; the rest stay countable only.
MAX_FAILURE_LINES = 10


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(NAME, help=HELP)
    parser.add_argument(
        "folder",
        type=Path,
        metavar="FOLDER",
        help="folder to fix (recursive)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be stamped without writing anything",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        metavar="PATH",
        help="configuration file (default: per-user config)",
    )


def run(args: argparse.Namespace) -> int:
    if not supported():
        print("vc fix-dates: creation dates are only settable on macOS", file=sys.stderr)
        return EXIT_ERROR
    folder = args.folder.expanduser()
    if not folder.is_dir():
        print(f"vc fix-dates: folder {folder} is not a directory", file=sys.stderr)
        return EXIT_USAGE
    config = load_config(args.config)
    report = sweep_folder(
        folder,
        mode=config.capture_time.mode,
        timezone=timezone_for_config(config),
        dry_run=args.dry_run,
    )
    print(render_report(report), end="")
    return 0


def render_report(report: StampReport) -> str:
    """The human-readable summary: one line per count, then failing paths."""
    lines = [
        f"scanned: {report.scanned}",
        f"stamped: {report.stamped}",
        f"would stamp: {report.would_stamp}",
        f"already correct: {report.already_correct}",
        f"skipped, no capture time: {report.skipped_no_capture}",
        f"skipped, older than since: {report.skipped_older}",
        f"ignored: {report.ignored}",
        f"failed: {report.failed}",
    ]
    failures = [result for result in report.results if result.status is StampStatus.FAILED]
    for result in failures[:MAX_FAILURE_LINES]:
        lines.append(f"failed: {result.path}: {result.error}")
    hidden = len(failures) - MAX_FAILURE_LINES
    if hidden > 0:
        lines.append(f"failed: … and {hidden} more")
    return "\n".join(lines) + "\n"
