"""``vc gui`` stub; issue #8 replaces this module with the PySide6 launch command."""

from __future__ import annotations

import argparse
import sys

from videocompressor.cli.constants import EXIT_NOT_IMPLEMENTED

NAME = "gui"
HELP = "launch the graphical interface (issue #8)"


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    subparsers.add_parser(NAME, help=HELP)


def run(args: argparse.Namespace) -> int:
    print(f"vc {NAME}: not implemented yet", file=sys.stderr)
    return EXIT_NOT_IMPLEMENTED
