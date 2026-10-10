"""``vc gui``: launch the PySide6 graphical interface (issues #8 and #9).

The GUI hosts the controller in-process (or attaches to one already serving the
configured address) and shows the library plus the job monitor.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path

from videocompressor.cli.constants import EXIT_ERROR

NAME = "gui"
HELP = "launch the graphical interface"


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(NAME, help=HELP)
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        metavar="PATH",
        help="configuration file (default: per-user config)",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        metavar="PATH",
        help="manifest database (default: per-user state)",
    )


def _load_run_gui() -> Callable[..., int]:
    """Import the PySide6 app lazily so ``vc`` works without the gui extra."""
    from videocompressor.gui.monitor_app import run as run_gui

    return run_gui


def run(args: argparse.Namespace) -> int:
    try:
        run_gui = _load_run_gui()
    except ModuleNotFoundError as error:
        if (error.name or "").partition(".")[0] != "PySide6":
            raise
        print("vc gui: the graphical interface needs the optional gui extra", file=sys.stderr)
        print("  install it with: pip install -e '.[gui]'", file=sys.stderr)
        return EXIT_ERROR
    return run_gui(config_path=args.config, manifest_path=args.manifest)
