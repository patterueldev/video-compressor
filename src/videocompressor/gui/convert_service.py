"""Convert-flow selection, destination, and enqueue payloads for the GUI (issue #41).

Pure Python — no PySide6 import — so the file collection rules, the destination
validation, and the enqueue payload shape stay testable in CI without the
optional ``gui`` extra. The convert panel is a thin rendering layer over this
module, and the controller API contract (issue #39) is the source of truth for
the payload and the per-file ``queued``/``skipped``/``rejected`` results.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from videocompressor.core.config import root_for_folder
from videocompressor.core.models import (
    ArchiveRoot,
    Destination,
    DestinationKind,
    destination_to_dict,
    is_safe_relative_path,
)

__all__ = [
    "AddOutcome",
    "ConvertSelection",
    "EnqueueOutcome",
    "MODE_CUSTOM_FOLDER",
    "MODE_OUTPUT_ROOT",
    "MODE_SAME_FOLDER",
    "REASON_LABELS",
    "SelectionEntry",
    "VIDEO_SUFFIXES",
    "build_enqueue_payload",
    "destination_from_fields",
    "effective_delete_original",
    "is_video",
    "status_text",
]

#: Video files a drop or dialog contributes (the plan.md scan set).
VIDEO_SUFFIXES = frozenset({".mp4", ".mov"})

PROBLEM_MISSING = "the file no longer exists"
PROBLEM_UNREADABLE = "the file cannot be read"
PROBLEM_OUTSIDE_ROOTS = "not under a registered root"

MODE_OUTPUT_ROOT = "output_root"
MODE_CUSTOM_FOLDER = "custom_folder"
MODE_SAME_FOLDER = "same_folder"

#: Human labels for the controller's per-file reason slugs (issue #39 contract).
REASON_LABELS: dict[str, str] = {
    "already_complete": "already complete",
    "already_queued": "already queued",
    "needs_review": "needs review",
    "previously_failed": "previously failed",
    "duplicate_in_batch": "duplicate in batch",
    "unknown_root": "unknown root",
    "unsafe_relative_path": "unsafe path",
    "not_a_file": "not a file",
    "unreadable": "unreadable",
}


def is_video(path: Path) -> bool:
    """Whether ``path`` has a video suffix (case-insensitive allowlist)."""
    return path.suffix.lower() in VIDEO_SUFFIXES


@dataclass(frozen=True)
class SelectionEntry:
    """One pending-list row: the picked file plus its archive mapping."""

    path: Path
    size_bytes: int | None
    root_id: str | None
    relative_path: str | None
    problem: str | None = None
    status: str | None = None

    @property
    def ready(self) -> bool:
        """Whether this entry can be sent to the enqueue API."""
        return (
            self.problem is None
            and self.root_id is not None
            and self.relative_path is not None
        )


@dataclass(frozen=True)
class AddOutcome:
    """Result of one selection addition, for the panel's status message."""

    added: int
    ignored_non_video: int
    duplicates: int


@dataclass(frozen=True)
class EnqueueOutcome:
    """Batch counts returned by ``POST /api/v1/jobs``."""

    queued: int = 0
    skipped: int = 0
    rejected: int = 0

    @property
    def total(self) -> int:
        return self.queued + self.skipped + self.rejected


class ConvertSelection:
    """The pending list: collected videos resolved against the registered roots.

    ``roots`` is read through a callable so roots registered later on the
    Library tab are picked up without rebuilding the selection.
    """

    def __init__(self, roots: Callable[[], Sequence[ArchiveRoot]]) -> None:
        self._roots = roots
        self._entries: list[SelectionEntry] = []
        self._keys: set[str] = set()

    # -- reading -----------------------------------------------------------

    def entries(self) -> tuple[SelectionEntry, ...]:
        """Every pending row, in addition order."""
        return tuple(self._entries)

    def ready_entries(self) -> tuple[SelectionEntry, ...]:
        """Rows the enqueue payload may carry."""
        return tuple(entry for entry in self._entries if entry.ready)

    def total_bytes(self) -> int:
        """Summed size of the pending rows (known sizes only)."""
        return sum(entry.size_bytes or 0 for entry in self._entries)

    # -- mutating ----------------------------------------------------------

    def clear(self) -> None:
        """Drop every pending row."""
        self._entries.clear()
        self._keys.clear()

    def add_paths(self, paths: Iterable[Path]) -> AddOutcome:
        """Append ``paths`` (files and folders), expanding folders to videos."""
        roots = tuple(self._roots())
        videos, ignored, missing = _collect_paths(paths)
        added = 0
        duplicates = 0
        candidates = [(video, None) for video in videos]
        candidates.extend((path, PROBLEM_MISSING) for path in missing)
        for path, problem in candidates:
            key = str(path.expanduser().resolve())
            if key in self._keys:
                duplicates += 1
                continue
            self._keys.add(key)
            self._entries.append(_make_entry(path, roots, problem))
            added += 1
        return AddOutcome(added=added, ignored_non_video=ignored, duplicates=duplicates)

    def apply_enqueue_result(self, payload: Mapping[str, Any]) -> EnqueueOutcome:
        """Apply a ``POST /jobs`` response to the matching rows; defensive."""
        results = _result_map(payload.get("jobs"))
        if results:
            for index, entry in enumerate(self._entries):
                if entry.root_id is None or entry.relative_path is None:
                    continue
                found = results.get((entry.root_id, entry.relative_path))
                if found is None:
                    continue
                status, reason = found
                self._entries[index] = replace(entry, status=status_text(status, reason))
        return EnqueueOutcome(
            queued=_count(payload.get("queued")),
            skipped=_count(payload.get("skipped")),
            rejected=_count(payload.get("rejected")),
        )


def destination_from_fields(
    *,
    mode: str,
    root_id: str | None,
    subfolder: str,
    folder: Path | None,
    roots: Sequence[ArchiveRoot],
) -> tuple[Destination | None, str | None]:
    """Build the core destination for the chosen mode.

    Returns ``(destination, None)`` or ``(None, problem)`` with a message the
    panel can show inline. The endpoint accepts ``output_root`` and
    ``same_folder`` only, so a custom folder resolves through the registered
    roots (``root_for_folder``) into an ``output_root`` spec; a folder that is
    a root itself resolves to the mirror form (empty relative path). A folder
    outside the roots is registered on Start by
    :func:`videocompressor.gui.root_service.resolve_destination`.
    """
    if mode == MODE_SAME_FOLDER:
        return Destination(kind=DestinationKind.SAME_FOLDER), None
    if mode == MODE_OUTPUT_ROOT:
        if root_id is None or all(root.id != root_id for root in roots):
            return None, "choose a registered output root (register one on the Library tab)"
        clean = subfolder.strip()
        if not is_safe_relative_path(clean, allow_empty=True):
            return None, "the subfolder must be a relative path without '.' or '..' segments"
        try:
            return Destination(DestinationKind.OUTPUT_ROOT, root_id, clean), None
        except ValueError as error:  # defensive; the checks above cover the inputs
            return None, str(error)
    if mode == MODE_CUSTOM_FOLDER:
        if folder is None:
            return None, "choose an output folder"
        root = root_for_folder(roots, folder)
        if root is None:
            return (
                None,
                "the output folder is not under a registered root;"
                " it will be registered when you press Start",
            )
        relative = _relative_to_root(folder, root)
        if relative is None:
            return None, "the output folder must be inside a registered root"
        if relative == ".":
            # The folder is the root itself: the empty relative path is the
            # mirror form, keeping each clip's source folders under it.
            return Destination(DestinationKind.OUTPUT_ROOT, root.id, ""), None
        return Destination(DestinationKind.OUTPUT_ROOT, root.id, relative), None
    return None, f"unknown destination mode {mode!r}"


def effective_delete_original(mode: str, requested: bool) -> bool:
    """The delete flag the payload must carry; same-folder always consumes the source."""
    return True if mode == MODE_SAME_FOLDER else bool(requested)


def build_enqueue_payload(
    entries: Sequence[SelectionEntry],
    destination: Destination,
    *,
    template_id: str,
    delete_original: bool,
) -> dict[str, object]:
    """The exact ``POST /api/v1/jobs`` body for the ready entries."""
    return {
        "files": [
            {"root_id": entry.root_id, "relative_path": entry.relative_path}
            for entry in entries
            if entry.ready
        ],
        "destination": destination_to_dict(destination),
        "template_id": template_id,
        "delete_original": delete_original,
    }


def status_text(status: str, reason: str | None) -> str:
    """``queued`` / ``skipped: already complete`` / ``rejected: not a file``."""
    if status in {"skipped", "rejected"}:
        label = REASON_LABELS.get(reason or "", reason or "unknown reason")
        return f"{status}: {label}"
    return status


# -- collection helpers ----------------------------------------------------


def _collect_paths(paths: Iterable[Path]) -> tuple[list[Path], int, list[Path]]:
    """Split ``paths`` into videos, ignored non-videos, and missing entries."""
    videos: list[Path] = []
    missing: list[Path] = []
    ignored = 0
    for raw in paths:
        path = Path(raw).expanduser()
        if path.is_symlink():
            ignored += 1
        elif path.is_dir():
            found, dismissed = _walk_videos(path)
            videos.extend(found)
            ignored += dismissed
        elif path.is_file():
            if is_video(path):
                videos.append(path)
            else:
                ignored += 1
        else:
            missing.append(path)
    videos.sort(key=lambda item: str(item))
    return videos, ignored, missing


def _walk_videos(root: Path) -> tuple[list[Path], int]:
    """Recursively collect videos under ``root``; symlinks are skipped."""
    found: list[Path] = []
    ignored = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(
            name for name in dirnames if not (Path(dirpath) / name).is_symlink()
        )
        for name in sorted(filenames):
            candidate = Path(dirpath) / name
            if candidate.is_symlink():
                ignored += 1
            elif is_video(candidate):
                found.append(candidate)
            else:
                ignored += 1
    return found, ignored


def _make_entry(path: Path, roots: Sequence[ArchiveRoot], problem: str | None) -> SelectionEntry:
    """Map one collected path to its root and archive-relative position."""
    size = _size(path)
    root = root_for_folder(roots, path)
    relative = _relative_to_root(path, root) if root is not None else None
    if root is None or relative is None:
        return SelectionEntry(path, size, None, None, problem or PROBLEM_OUTSIDE_ROOTS)
    if problem is None and size is None:
        problem = PROBLEM_UNREADABLE
    return SelectionEntry(path, size, root.id, relative, problem)


def _relative_to_root(path: Path, root: ArchiveRoot) -> str | None:
    """The POSIX path of ``path`` inside ``root``, or ``None`` when outside."""
    try:
        return path.expanduser().resolve().relative_to(root.path.expanduser().resolve()).as_posix()
    except ValueError:
        return None


def _size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


def _result_map(value: object) -> dict[tuple[str, str], tuple[str, str | None]]:
    """Defensive parse of the per-file ``jobs`` results, keyed by clip identity."""
    results: dict[tuple[str, str], tuple[str, str | None]] = {}
    if not isinstance(value, list):
        return results
    for item in value:
        if not isinstance(item, Mapping):
            continue
        root_id = item.get("root_id")
        relative_path = item.get("relative_path")
        status = item.get("status")
        reason = item.get("reason")
        if not isinstance(root_id, str) or not isinstance(relative_path, str):
            continue
        if not isinstance(status, str):
            continue
        results[(root_id, relative_path)] = (
            status,
            reason if isinstance(reason, str) else None,
        )
    return results


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, value)
