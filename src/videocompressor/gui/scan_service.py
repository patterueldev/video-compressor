"""Read-only dry-run service behind the GUI's scan action (issue #8).

Pure Python — no PySide6 import. The manifest policy mirrors ``vc scan``: the
manifest is opened read-only and only when the file exists, any SQLite failure
aborts the run with :class:`ScanManifestError`, and the connection is always
closed. A dry run never writes: no manifest rows, no folder markers, no report
file unless the operator asks the CLI for one.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from videocompressor.core.manifest import Manifest
from videocompressor.core.models import ArchiveRoot, CaptureTimeMode, TemplateSettings
from videocompressor.core.probe import FFprobeUnavailableError
from videocompressor.core.scan import Inspector, ScanReport, scan_folder


class ScanManifestError(RuntimeError):
    """The manifest file exists but cannot be used read-only."""


@dataclass(frozen=True)
class ScanRequest:
    """Everything one dry run needs, resolved before it leaves the GUI thread."""

    root: ArchiveRoot
    folder: Path
    template_id: str
    settings: TemplateSettings
    capture_mode: CaptureTimeMode
    manifest_path: Path | None


def run_scan(request: ScanRequest, *, inspector: Inspector | None = None) -> ScanReport:
    """Classify ``request.folder`` under ``request.root`` without writing anything."""
    path = request.manifest_path
    if path is None or not path.is_file():
        return _scan(request, manifest=None, inspector=inspector)
    try:
        manifest = Manifest.open(path, create=False)
    except sqlite3.Error as error:
        raise ScanManifestError(f"cannot open manifest {path}: {error}") from error
    try:
        return _scan(request, manifest=manifest, inspector=inspector)
    except sqlite3.Error as error:
        raise ScanManifestError(f"cannot read manifest {path}: {error}") from error
    finally:
        manifest.close()


def _scan(
    request: ScanRequest, *, manifest: Manifest | None, inspector: Inspector | None
) -> ScanReport:
    return scan_folder(
        root_id=request.root.id,
        root=request.root.path,
        folder=request.folder,
        template_id=request.template_id,
        settings=request.settings,
        capture_mode=request.capture_mode,
        manifest=manifest,
        inspector=inspector,
    )


def describe_scan_error(error: BaseException) -> str:
    """One-line message for the GUI's error dialog."""
    if isinstance(error, FFprobeUnavailableError):
        return "ffprobe was not found; install FFmpeg and try again"
    if isinstance(error, (ScanManifestError, ValueError, OSError)):
        return str(error)
    return f"unexpected error: {error!r}"
