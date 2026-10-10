"""Command-line entry points for Video Compressor.

``vc probe`` (issue #2) and ``vc scan`` (issue #3) are implemented and
read-only; they never write into the archive. ``vc controller`` (issue #7) runs
the local controller. The remaining subcommands belong to later issues; until
those land, they exit with a clear "not implemented" message (exit code 2).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

from videocompressor import __version__
from videocompressor.controller.server import ControllerServer, new_token, open_manifest
from videocompressor.controller.store import ControllerStore, default_db_path
from videocompressor.core.capture_time import bucket_for, resolve_capture_time
from videocompressor.core.config import (
    DEFAULT_TEMPLATE_NAME,
    default_manifest_path,
    load_config,
    save_config,
)
from videocompressor.core.manifest import Manifest
from videocompressor.core.models import (
    Bucket,
    CaptureIssue,
    CaptureTime,
    CaptureTimeMode,
    TemplateSettings,
)
from videocompressor.core.probe import FFprobeUnavailableError, ProbeError, probe_media
from videocompressor.core.scan import (
    DRY_RUN_REPORT_FILENAME,
    derive_root_id,
    render_table,
    scan_folder,
    write_report,
)

EXIT_ERROR = 1
EXIT_NOT_IMPLEMENTED = 2
EXIT_USAGE = 2

#: Subcommands stubbed here; the owning issue implements each one.
_SUBCOMMANDS = {
    "worker": "run the worker agent for this node (issue #6)",
    "controller": "run the local controller and worker registry",
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
        subparser = subparsers.add_parser(name, help=help_text)
        if name == "controller":
            subparser.add_argument(
                "--host", help="bind address (default: the config value, 127.0.0.1)"
            )
            subparser.add_argument(
                "--port", type=int, help="bind port (default: the config value, 8765)"
            )
            subparser.add_argument(
                "--config", type=Path, help="config file path (default: the per-user path)"
            )
            subparser.add_argument(
                "--db", type=Path, help="controller database path (default: the per-user path)"
            )
            subparser.add_argument(
                "--print-token",
                action="store_true",
                help="print the access token (generating one if needed) and exit",
            )

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

    if args.command == "probe":
        return _run_probe(args.file)

    if args.command == "controller":
        return _run_controller(args)

    print(f"vc {args.command}: not implemented yet", file=sys.stderr)
    return EXIT_NOT_IMPLEMENTED


def _run_controller(args: argparse.Namespace) -> int:
    if args.port is not None and not 0 <= args.port <= 65535:
        print("vc controller: --port must be between 0 and 65535", file=sys.stderr)
        return 2

    config = load_config(args.config)
    if args.print_token:
        if config.controller.token is None:
            config.controller.token = new_token()
            save_config(config, args.config)
        print(config.controller.token)
        return 0

    if config.controller.token is None:
        config.controller.token = new_token()
        saved = save_config(config, args.config)
        print(f"vc controller: generated an access token and saved it to {saved}", file=sys.stderr)

    host = args.host or config.controller.host
    port = args.port if args.port is not None else config.controller.port
    store = ControllerStore.open(args.db or default_db_path())
    manifest_path = default_manifest_path()
    manifest, durable = open_manifest(manifest_path)
    server = ControllerServer(
        store=store,
        manifest=manifest,
        token=config.controller.token,
        host=host,
        port=port,
        templates=config.templates,
    )
    server.start_background()
    print(f"vc controller: listening on {server.base_url}")
    if durable:
        print(f"vc controller: queue: durable manifest at {manifest_path}")
    else:
        print(
            "vc controller: queue: in-memory stub (the manifest could not be opened)"
            " - queued clips will NOT survive a restart",
            file=sys.stderr,
        )
    print("vc controller: press Ctrl-C to stop")
    try:
        server.wait()
    except KeyboardInterrupt:
        print("vc controller: stopping", file=sys.stderr)
    finally:
        server.stop()
    return 0


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
