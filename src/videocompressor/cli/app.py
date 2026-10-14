"""Parser assembly, dispatch, and the single command registry for ``vc``.

``vc probe`` (issue #2) and ``vc scan`` (issue #3) are implemented and read-only;
``vc controller`` (issue #7) runs the local controller. The remaining subcommands
belong to later issues; until those land, their stub modules exit with a clear
"not implemented" message (exit code 2).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import Protocol

from videocompressor import __version__
from videocompressor.cli import controller, fixdates, gui, node, probe, scan, worker
from videocompressor.cli.constants import EXIT_NOT_IMPLEMENTED


class CommandModule(Protocol):
    """Minimal seam every ``vc`` subcommand module implements."""

    NAME: str
    HELP: str

    def add_parser(self, subparsers: argparse._SubParsersAction) -> None: ...

    def run(self, args: argparse.Namespace) -> int: ...


#: Single command registry; order drives ``vc --help``.
COMMANDS: tuple[CommandModule, ...] = (probe, scan, fixdates, worker, node, controller, gui)

_COMMANDS_BY_NAME = {command.NAME: command for command in COMMANDS}


def build_parser() -> argparse.ArgumentParser:
    """Build the ``vc`` argument parser."""
    parser = argparse.ArgumentParser(
        prog="vc",
        description="GUI-driven, resumable GoPro video compressor.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    for command in COMMANDS:
        command.add_parser(subparsers)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return the process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_usage(sys.stderr)
        print("vc: choose a command (see 'vc --help')", file=sys.stderr)
        return EXIT_NOT_IMPLEMENTED

    return _COMMANDS_BY_NAME[args.command].run(args)
