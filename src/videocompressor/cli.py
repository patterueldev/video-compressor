"""Command-line entry points for Video Compressor.

``vc scan`` runs a read-only dry run over a footage folder inside an archive
root and writes only the report. The remaining subcommands belong to later
issues; until those land, they exit with a clear "not implemented" message
(exit code 2) instead of doing nothing.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

from videocompressor import __version__
from videocompressor.core.config import (
    DEFAULT_TEMPLATE_NAME,
    default_manifest_path,
    load_config,
)
from videocompressor.core.manifest import Manifest
from videocompressor.core.scan import (
    DRY_RUN_REPORT_FILENAME,
    derive_root_id,
    load_capture_resolver,
    probe_inspector,
    render_table,
    scan_folder,
    write_report,
)

EXIT_NOT_IMPLEMENTED = 2
EXIT_USAGE = 2
EXIT_ERROR = 1

#: Subcommands stubbed here; the owning issue implements each one.
_SUBCOMMANDS = {
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
    scan = subparsers.add_parser(
        "scan", help="scan a footage folder and report a dry run (issue #3)"
    )
    scan.add_argument(
        "--root",
        required=True,
        type=Path,
        metavar="PATH",
        help="archive root that contains the folder",
    )
    scan.add_argument(
        "--folder",
        required=True,
        type=Path,
        metavar="PATH",
        help="footage folder to scan (must live inside --root)",
    )
    scan.add_argument(
        "--root-id",
        default=None,
        help="manifest id of the archive root (default: derived from --root)",
    )
    scan.add_argument(
        "--template",
        default=DEFAULT_TEMPLATE_NAME,
        help="encoding template name (default: %(default)s)",
    )
    scan.add_argument(
        "--manifest",
        type=Path,
        default=None,
        metavar="PATH",
        help="manifest database (default: per-user state)",
    )
    scan.add_argument(
        "--config",
        type=Path,
        default=None,
        metavar="PATH",
        help="configuration file (default: per-user config)",
    )
    scan.add_argument(
        "--report",
        type=Path,
        default=Path(DRY_RUN_REPORT_FILENAME),
        metavar="PATH",
        help="dry-run report output (default: %(default)s)",
    )

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
    if args.command == "scan":
        return _run_scan(args)

    print(f"vc {args.command}: not implemented yet", file=sys.stderr)
    return EXIT_NOT_IMPLEMENTED


def _run_scan(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    settings = config.templates.get(args.template)
    if settings is None:
        available = ", ".join(sorted(config.templates)) or "none"
        message = f"vc scan: unknown template {args.template!r} (available: {available})"
        print(message, file=sys.stderr)
        return EXIT_USAGE
    if not args.root.is_dir():
        print(f"vc scan: archive root {args.root} is not a directory", file=sys.stderr)
        return EXIT_USAGE
    if not args.folder.is_dir():
        print(f"vc scan: folder {args.folder} is not a directory", file=sys.stderr)
        return EXIT_USAGE

    root_id = args.root_id if args.root_id else derive_root_id(args.root)
    manifest_path = args.manifest if args.manifest is not None else default_manifest_path()
    manifest: Manifest | None = None
    if manifest_path.is_file():
        try:
            manifest = Manifest.open(manifest_path, create=False)
        except sqlite3.Error as error:
            print(f"vc scan: cannot open manifest {manifest_path}: {error}", file=sys.stderr)
            return EXIT_ERROR
    else:
        print(f"vc scan: no manifest at {manifest_path}; classifying without it", file=sys.stderr)

    capture_resolver = load_capture_resolver(
        mode=config.capture_time.mode, timezone_name=settings.timezone
    )
    if capture_resolver is None:
        print(
            "vc scan: capture-time support (issue #2) is not merged; "
            "non-complete clips will show as review",
            file=sys.stderr,
        )

    try:
        try:
            report = scan_folder(
                root_id=root_id,
                root=args.root,
                folder=args.folder,
                template_id=args.template,
                settings=settings,
                manifest=manifest,
                inspector=probe_inspector(capture_resolver=capture_resolver),
            )
        except ValueError as error:
            print(f"vc scan: {error}", file=sys.stderr)
            return EXIT_USAGE
        sys.stdout.write(render_table(report))
        try:
            write_report(report, args.report)
        except OSError as error:
            print(f"vc scan: cannot write report {args.report}: {error}", file=sys.stderr)
            return EXIT_ERROR
        print(f"Report written to {args.report}")
    finally:
        if manifest is not None:
            manifest.close()
    return 0
