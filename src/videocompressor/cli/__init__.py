"""``vc`` command-line entry points for Video Compressor."""

from __future__ import annotations

from videocompressor.cli.app import build_parser, main
from videocompressor.cli.constants import EXIT_ERROR, EXIT_NOT_IMPLEMENTED, EXIT_USAGE

__all__ = [
    "EXIT_ERROR",
    "EXIT_NOT_IMPLEMENTED",
    "EXIT_USAGE",
    "build_parser",
    "main",
]
