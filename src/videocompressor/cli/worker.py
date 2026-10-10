"""``vc worker`` stub; issue #6 replaces this module with the worker agent command."""

from __future__ import annotations

import argparse
import sys

from videocompressor.cli.constants import EXIT_NOT_IMPLEMENTED

NAME = "worker"
HELP = "run the worker agent for this node (issue #6)"


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    subparsers.add_parser(NAME, help=HELP)


def run(args: argparse.Namespace) -> int:
    print(f"vc {NAME}: not implemented yet", file=sys.stderr)
    return EXIT_NOT_IMPLEMENTED
