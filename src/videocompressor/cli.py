"""Command-line entry points for Video Compressor.

Each subcommand belongs to a later issue. Until that issue lands, the stub exits
with a clear "not implemented" message (exit code 2) instead of doing nothing.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from videocompressor import __version__

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

    print(f"vc {args.command}: not implemented yet", file=sys.stderr)
    return EXIT_NOT_IMPLEMENTED


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
