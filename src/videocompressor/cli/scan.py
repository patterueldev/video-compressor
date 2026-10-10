"""``vc scan``: scan a footage folder and report a dry run (issue #3)."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from videocompressor.cli.constants import EXIT_ERROR, EXIT_USAGE
from videocompressor.core.config import (
    DEFAULT_TEMPLATE_NAME,
    default_manifest_path,
    load_config,
)
from videocompressor.core.manifest import Manifest
from videocompressor.core.probe import FFprobeUnavailableError
from videocompressor.core.scan import (
    DRY_RUN_REPORT_FILENAME,
    derive_root_id,
    render_table,
    scan_folder,
    write_report,
)

NAME = "scan"
HELP = "scan a footage folder and report a dry run (issue #3)"


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(NAME, help=HELP)
    parser.add_argument(
        "--root",
        required=True,
        type=Path,
        metavar="PATH",
        help="archive root that contains the folder",
    )
    parser.add_argument(
        "--folder",
        required=True,
        type=Path,
        metavar="PATH",
        help="footage folder to scan (must live inside --root)",
    )
    parser.add_argument(
        "--root-id",
        default=None,
        help="manifest id of the archive root (default: derived from --root)",
    )
    parser.add_argument(
        "--template",
        default=DEFAULT_TEMPLATE_NAME,
        help="encoding template name (default: %(default)s)",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        metavar="PATH",
        help="manifest database (default: per-user state)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        metavar="PATH",
        help="configuration file (default: per-user config)",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path(DRY_RUN_REPORT_FILENAME),
        metavar="PATH",
        help="dry-run report output (default: %(default)s)",
    )


def run(args: argparse.Namespace) -> int:
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

    try:
        try:
            report = scan_folder(
                root_id=root_id,
                root=args.root,
                folder=args.folder,
                template_id=args.template,
                settings=settings,
                capture_mode=config.capture_time.mode,
                manifest=manifest,
            )
        except FFprobeUnavailableError as error:
            print(f"vc scan: {error}", file=sys.stderr)
            return EXIT_ERROR
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
