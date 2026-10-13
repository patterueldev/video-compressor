"""Root auto-registration and the root-removal guard for the GUI (issue #82).

Pure Python — no PySide6 import — so the rules stay testable in CI without the
optional ``gui`` extra. The convert flow drops the pre-registration ceremony: a
dropped or picked folder outside the registered roots becomes a new root (id
from :func:`videocompressor.core.scan.derive_root_id`, name from the folder
name) and is persisted to the per-user config once per batch; a picked output
folder registers on use and resolves to the mirror form (the new root with an
empty relative path). The Library roots panel refuses to remove a root while
the local manifest holds non-terminal clips for it, reading that manifest
strictly read-only — removal only rewrites the app config, never manifest
history.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from videocompressor.core.config import (
    ROOT_ID_PATTERN,
    AppConfig,
    register_root,
    root_for_folder,
    save_config,
    validate_root_id,
)
from videocompressor.core.manifest import Manifest
from videocompressor.core.models import ArchiveRoot, Destination, JobState
from videocompressor.core.nodes import NodeEntry
from videocompressor.core.scan import derive_root_id
from videocompressor.gui.convert_service import (
    MODE_CUSTOM_FOLDER,
    SelectionEntry,
    destination_from_fields,
)

__all__ = [
    "NON_TERMINAL_STATES",
    "OutputResolution",
    "RootRegistrationOutcome",
    "candidate_folders",
    "format_node_warning",
    "missing_node_roots",
    "register_auto_root",
    "register_roots_for_paths",
    "registration_note",
    "remove_root",
    "resolve_destination",
    "root_job_counts",
    "unique_root_id",
    "used_root_ids",
]

#: Clip states that block removing their root: everything except the settled
#: and parked states (complete, failed, review).
NON_TERMINAL_STATES = frozenset(JobState) - {
    JobState.COMPLETE,
    JobState.FAILED,
    JobState.REVIEW,
}

#: Root ids double as manifest keys, so the derived id is capped at this many
#: characters; the trailing ``-<8 hex>`` hash stays intact.
MAX_ROOT_ID_LENGTH = 64


@dataclass(frozen=True)
class RootRegistrationOutcome:
    """What one auto-registration pass did, for the panel's status message."""

    registered: tuple[ArchiveRoot, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class OutputResolution:
    """The Start-time destination, registering a custom folder when needed."""

    destination: Destination | None = None
    problem: str | None = None
    registered: ArchiveRoot | None = None
    error: str | None = None


def candidate_folders(paths: Iterable[Path]) -> tuple[Path, ...]:
    """The minimal folder to register for each dropped or picked path.

    A folder registers itself; a file registers its containing folder. Symlinks
    (the collector skips them), missing paths, and filesystem roots (which
    would swallow every folder) are ignored. Candidates are deduplicated and
    sorted shallowest-first, so a folder dropped with one of its parents
    resolves under the parent instead of registering twice.
    """
    candidates: dict[str, Path] = {}
    for raw in paths:
        path = Path(raw).expanduser()
        if path.is_symlink():
            continue
        if path.is_dir():
            folder = path
        elif path.is_file():
            folder = path.parent
        else:
            continue
        resolved = folder.resolve()
        if resolved.parent == resolved:
            continue
        candidates[str(resolved)] = resolved
    return tuple(
        sorted(candidates.values(), key=lambda item: (len(item.parts), str(item)))
    )


def register_roots_for_paths(
    config: AppConfig, paths: Iterable[Path], *, config_path: Path
) -> RootRegistrationOutcome:
    """Register a root for every candidate folder not inside a registered root.

    The config is saved once per call; a failed write rolls back every root
    this call appended, exactly like the manual registration path.
    """
    registered: list[ArchiveRoot] = []
    problems: list[str] = []
    count = len(config.roots)
    for folder in candidate_folders(paths):
        if root_for_folder(config.roots, folder) is not None:
            continue
        try:
            registered.append(_build_root(config, folder))
        except ValueError as error:
            problems.append(f"{folder}: {error}")
    if registered:
        try:
            save_config(config, config_path)
        except OSError as error:
            del config.roots[count:]
            return RootRegistrationOutcome(
                error=f"cannot save the configuration: {error}"
            )
    return RootRegistrationOutcome(
        registered=tuple(registered), error="; ".join(problems) or None
    )


def register_auto_root(
    config: AppConfig, folder: Path, *, config_path: Path
) -> tuple[ArchiveRoot | None, str | None]:
    """Register one folder as a root; ``(root, None)`` or ``(None, error)``.

    A folder already covered by a registered root returns that root without a
    second registration, so callers can retry safely.
    """
    resolved = folder.expanduser().resolve()
    existing = root_for_folder(config.roots, resolved)
    if existing is not None:
        return existing, None
    count = len(config.roots)
    try:
        root = _build_root(config, resolved)
    except ValueError as error:
        return None, str(error)
    try:
        save_config(config, config_path)
    except OSError as error:
        del config.roots[count:]
        return None, f"cannot save the configuration: {error}"
    return root, None


def unique_root_id(config: AppConfig, folder: Path) -> str:
    """A valid, unclaimed root id for ``folder``: slug + short hash, ≤ 64 chars.

    Derived ids that collide with a different registered path fall back to a
    ``-2``, ``-3``, … suffix.
    """
    base = _fit_root_id(derive_root_id(folder.resolve()))
    taken = {root.id for root in config.roots}
    if base not in taken:
        return base
    for suffix in range(2, 1000):
        candidate = f"{base[: MAX_ROOT_ID_LENGTH - len(str(suffix)) - 1]}-{suffix}"
        if candidate not in taken:
            return candidate
    raise ValueError(f"cannot derive a free root id for {folder}")  # pragma: no cover


def resolve_destination(
    config: AppConfig,
    *,
    mode: str,
    root_id: str | None,
    subfolder: str,
    folder: Path | None,
    config_path: Path,
) -> OutputResolution:
    """Resolve the Start-time destination, registering a custom folder on use.

    Non-custom modes and already-resolvable custom folders delegate to
    :func:`videocompressor.gui.convert_service.destination_from_fields`. A
    custom folder outside the registered roots is registered (the folder
    itself) and re-resolved to the mirror form: the new root with an empty
    relative path keeps each clip's source folders under the picked folder.
    """
    destination, problem = destination_from_fields(
        mode=mode, root_id=root_id, subfolder=subfolder, folder=folder, roots=config.roots
    )
    if problem is None or mode != MODE_CUSTOM_FOLDER or folder is None:
        return OutputResolution(destination=destination, problem=problem)
    expanded = folder.expanduser()
    if not expanded.is_dir():
        return OutputResolution(problem="the output folder must be an existing folder")
    root, error = register_auto_root(config, expanded, config_path=config_path)
    if root is None:
        return OutputResolution(
            problem=f"the output folder could not be registered: {error}", error=error
        )
    destination, problem = destination_from_fields(
        mode=mode, root_id=None, subfolder=subfolder, folder=expanded, roots=config.roots
    )
    return OutputResolution(destination=destination, problem=problem, registered=root)


def remove_root(config: AppConfig, root_id: str) -> ArchiveRoot:
    """Remove and return the root with ``root_id`` (``ValueError`` when unknown)."""
    for index, root in enumerate(config.roots):
        if root.id == root_id:
            return config.roots.pop(index)
    raise ValueError(f"unknown root {root_id!r}")


def root_job_counts(manifest_path: Path | None, root_id: str) -> dict[str, int] | None:
    """Non-terminal clip counts per state for one root.

    ``{}`` when there is no manifest history, ``None`` when the manifest cannot
    be read (the caller then asks for explicit confirmation instead of silently
    removing the root). The manifest is opened strictly read-only.
    """
    if manifest_path is None:
        return None
    if not manifest_path.is_file():
        return {}
    try:
        with Manifest.open(manifest_path, create=False) as manifest:
            records = manifest.list_clips()
    except (OSError, sqlite3.Error):
        return None
    counts: dict[str, int] = {}
    for record in records:
        if record.root_id != root_id or record.state not in NON_TERMINAL_STATES:
            continue
        key = str(record.state)
        counts[key] = counts.get(key, 0) + 1
    return counts


def missing_node_roots(
    nodes: Sequence[NodeEntry], root_ids: Iterable[str]
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Configured nodes whose ``roots`` mapping lacks any of ``root_ids``.

    Returns ``(display_name, missing_root_ids)`` per node; the local worker is
    not a ``[[nodes]]`` entry (its mapping follows the app config).
    """
    wanted = tuple(dict.fromkeys(root_ids))
    missing: list[tuple[str, tuple[str, ...]]] = []
    for node in nodes:
        absent = tuple(root_id for root_id in wanted if root_id not in node.roots)
        if absent:
            missing.append((node.display_name, absent))
    return tuple(missing)


def format_node_warning(
    missing: Sequence[tuple[str, tuple[str, ...]]],
) -> str | None:
    """One status line for nodes that cannot process a root used by the batch."""
    if not missing:
        return None
    names = ", ".join(f"{name!r}" for name, _ in missing)
    root_ids = tuple(dict.fromkeys(root_id for _, ids in missing for root_id in ids))
    labels = ", ".join(f"{root_id!r}" for root_id in root_ids)
    subject = f"node {names}" if len(missing) == 1 else f"nodes {names}"
    verb = "does not" if len(missing) == 1 else "do not"
    return (
        f"{subject} {verb} map {labels} yet; run vc node install to add the mapping"
    )


def registration_note(outcome: RootRegistrationOutcome) -> str | None:
    """The status note for one auto-registration pass; ``None`` when silent."""
    parts: list[str] = []
    if outcome.registered:
        if len(outcome.registered) == 1:
            root = outcome.registered[0]
            parts.append(f"registered new root {root.name!r} ({root.id})")
        else:
            parts.append(f"registered {len(outcome.registered)} new roots")
    if outcome.error:
        parts.append(outcome.error)
    return ", ".join(parts) or None


def used_root_ids(
    entries: Iterable[SelectionEntry], destination: Destination
) -> tuple[str, ...]:
    """Every root the enqueue payload touches: the files' roots plus the destination."""
    ids = {entry.root_id for entry in entries if entry.root_id is not None}
    if destination.root_id is not None:
        ids.add(destination.root_id)
    return tuple(sorted(ids))


def _build_root(config: AppConfig, folder: Path) -> ArchiveRoot:
    """Register ``folder`` under a derived, valid, unclaimed id."""
    if folder.parent == folder:
        raise ValueError("the filesystem root cannot be registered as a root")
    return register_root(
        config, root_id=unique_root_id(config, folder), name=folder.name, path=folder
    )


def _fit_root_id(value: str) -> str:
    """Shorten ``value`` to a valid root id, keeping its 8-hex hash suffix."""
    if len(value) <= MAX_ROOT_ID_LENGTH and ROOT_ID_PATTERN.fullmatch(value):
        return value
    slug, _, digest = value.rpartition("-")
    budget = MAX_ROOT_ID_LENGTH - len(digest) - 1
    slug = slug[:budget].strip("-") or "root"
    return validate_root_id(f"{slug}-{digest}" if digest else slug[:MAX_ROOT_ID_LENGTH])
