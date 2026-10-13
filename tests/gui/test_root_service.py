"""Root auto-registration and the removal guard (issue #82).

Pure Python: runs in CI without the optional ``gui`` extra.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from videocompressor.core.config import ROOT_ID_PATTERN, AppConfig, load_config, register_root
from videocompressor.core.manifest import Manifest
from videocompressor.core.models import (
    ArchiveRoot,
    ClipRecord,
    Destination,
    DestinationKind,
    Fingerprint,
    JobState,
)
from videocompressor.core.nodes import NodeEntry
from videocompressor.core.scan import derive_root_id
from videocompressor.gui.convert_service import (
    MODE_CUSTOM_FOLDER,
    MODE_OUTPUT_ROOT,
    MODE_SAME_FOLDER,
    SelectionEntry,
)
from videocompressor.gui.root_service import (
    NON_TERMINAL_STATES,
    RootRegistrationOutcome,
    candidate_folders,
    format_node_warning,
    missing_node_roots,
    register_auto_root,
    register_roots_for_paths,
    registration_note,
    remove_root,
    resolve_destination,
    root_job_counts,
    unique_root_id,
    used_root_ids,
)


def clip(path: Path, payload: bytes = b"x" * 64) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def make_root(path: Path, root_id: str) -> ArchiveRoot:
    return ArchiveRoot(id=root_id, name=root_id, path=path)


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    return tmp_path / "config.toml"


# -- candidate folders --------------------------------------------------------


def test_candidate_folders_uses_the_folder_or_a_files_parent(tmp_path):
    folder = tmp_path / "ride"
    folder.mkdir()
    video = clip(folder / "GS010001.MP4")

    assert candidate_folders([folder, video, video]) == (folder.resolve(),)


def test_candidate_folders_skips_symlinks_missing_and_filesystem_roots(tmp_path):
    real = clip(tmp_path / "real.MP4")
    link = tmp_path / "link.MP4"
    link.symlink_to(real)

    assert candidate_folders([link, tmp_path / "gone.MP4", Path("/")]) == ()


def test_candidate_folders_sorts_shallowest_first(tmp_path):
    parent = tmp_path / "footage"
    child = parent / "ride"
    child.mkdir(parents=True)

    assert candidate_folders([child, parent]) == (parent.resolve(), child.resolve())


# -- registration -------------------------------------------------------------


def test_register_roots_for_paths_registers_once_and_persists(config_path, tmp_path):
    config = AppConfig()
    folder = tmp_path / "ride"
    clip(folder / "GS010001.MP4")

    outcome = register_roots_for_paths(config, [folder], config_path=config_path)

    assert outcome.error is None
    (root,) = outcome.registered
    assert root.name == "ride"
    assert root.id == derive_root_id(folder.resolve())
    assert root.path == folder.resolve()
    assert load_config(config_path).roots == [root]

    again = register_roots_for_paths(config, [folder], config_path=config_path)
    assert again.registered == ()
    assert len(config.roots) == 1


def test_register_roots_for_paths_skips_folders_inside_roots(config_path, tmp_path):
    config = AppConfig()
    archive = tmp_path / "archive"
    archive.mkdir()
    register_root(config, root_id="archive", name="Archive", path=archive)
    inside = archive / "ride"
    inside.mkdir()

    outcome = register_roots_for_paths(config, [inside], config_path=config_path)

    assert outcome.registered == ()
    assert [root.id for root in config.roots] == ["archive"]


def test_register_roots_for_paths_registers_the_shallowest_candidate(config_path, tmp_path):
    config = AppConfig()
    parent = tmp_path / "footage"
    child = parent / "ride"
    child.mkdir(parents=True)
    clip(child / "GS010001.MP4")

    outcome = register_roots_for_paths(config, [child, parent], config_path=config_path)

    assert [root.path for root in outcome.registered] == [parent.resolve()]


def test_register_roots_for_paths_rolls_back_when_the_write_fails(tmp_path):
    config = AppConfig()
    folder = tmp_path / "ride"
    folder.mkdir()
    unwritable = tmp_path / "config.toml"
    unwritable.mkdir()  # opening this path for writing fails with OSError

    outcome = register_roots_for_paths(config, [folder], config_path=unwritable)

    assert outcome.registered == ()
    assert outcome.error is not None and "cannot save" in outcome.error
    assert config.roots == []


def test_unique_root_id_truncates_long_names_and_suffixes_collisions(config_path, tmp_path):
    config = AppConfig()
    long_name = "a" * 90
    folder = tmp_path / long_name
    folder.mkdir()

    root_id = unique_root_id(config, folder)

    assert len(root_id) <= 64
    assert ROOT_ID_PATTERN.fullmatch(root_id)

    other = tmp_path / "other"
    other.mkdir()
    register_root(config, root_id=root_id, name="Taken", path=other)

    colliding = unique_root_id(config, folder)

    assert colliding.endswith("-2")
    assert len(colliding) <= 64
    assert ROOT_ID_PATTERN.fullmatch(colliding)


def test_register_auto_root_is_idempotent(config_path, tmp_path):
    config = AppConfig()
    folder = tmp_path / "ride"
    folder.mkdir()

    root, error = register_auto_root(config, folder, config_path=config_path)
    assert error is None and root is not None and root.path == folder.resolve()

    again, error = register_auto_root(config, folder, config_path=config_path)
    assert error is None and again == root
    assert len(config.roots) == 1


def test_register_auto_root_reports_write_failure(tmp_path):
    config = AppConfig()
    folder = tmp_path / "ride"
    folder.mkdir()
    unwritable = tmp_path / "config.toml"
    unwritable.mkdir()

    root, error = register_auto_root(config, folder, config_path=unwritable)

    assert root is None and error is not None and "cannot save" in error
    assert config.roots == []


# -- output resolution --------------------------------------------------------


def test_resolve_destination_output_root_and_same_folder_unchanged(config_path, tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    config = AppConfig()
    register_root(config, root_id="archive", name="Archive", path=archive)

    destination, problem = destination_of(
        config,
        config_path,
        mode=MODE_OUTPUT_ROOT,
        root_id="archive",
        subfolder="Out",
    )
    assert problem is None
    assert destination == Destination(DestinationKind.OUTPUT_ROOT, "archive", "Out")

    same, problem = destination_of(config, config_path, mode=MODE_SAME_FOLDER)
    assert problem is None and same == Destination(DestinationKind.SAME_FOLDER)


def test_resolve_destination_custom_folder_inside_root_unchanged(config_path, tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    config = AppConfig()
    register_root(config, root_id="archive", name="Archive", path=archive)

    destination, problem = destination_of(
        config,
        config_path,
        mode=MODE_CUSTOM_FOLDER,
        folder=archive / "Out" / "2026-10",
    )

    assert problem is None
    assert destination == Destination(DestinationKind.OUTPUT_ROOT, "archive", "Out/2026-10")
    assert len(config.roots) == 1


def test_resolve_destination_custom_folder_that_is_the_root_mirrors(config_path, tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    config = AppConfig()
    register_root(config, root_id="archive", name="Archive", path=archive)

    destination, problem = destination_of(
        config, config_path, mode=MODE_CUSTOM_FOLDER, folder=archive
    )

    assert problem is None
    assert destination == Destination(DestinationKind.OUTPUT_ROOT, "archive", "")


def test_resolve_destination_registers_an_outside_folder_on_use(config_path, tmp_path):
    config = AppConfig()
    outside = tmp_path / "out"
    outside.mkdir()

    resolution = resolve_destination(
        config,
        mode=MODE_CUSTOM_FOLDER,
        root_id=None,
        subfolder="",
        folder=outside,
        config_path=config_path,
    )

    assert resolution.problem is None
    assert resolution.registered is not None
    assert resolution.registered.path == outside.resolve()
    assert resolution.destination == Destination(
        DestinationKind.OUTPUT_ROOT, resolution.registered.id, ""
    )
    assert load_config(config_path).roots == [resolution.registered]


def test_resolve_destination_reports_a_missing_folder(config_path, tmp_path):
    resolution = resolve_destination(
        AppConfig(),
        mode=MODE_CUSTOM_FOLDER,
        root_id=None,
        subfolder="",
        folder=tmp_path / "gone",
        config_path=config_path,
    )

    assert resolution.destination is None
    assert resolution.problem is not None and "existing folder" in resolution.problem


def test_resolve_destination_reports_a_failed_registration(tmp_path):
    config = AppConfig()
    outside = tmp_path / "out"
    outside.mkdir()
    unwritable = tmp_path / "config.toml"
    unwritable.mkdir()

    resolution = resolve_destination(
        config,
        mode=MODE_CUSTOM_FOLDER,
        root_id=None,
        subfolder="",
        folder=outside,
        config_path=unwritable,
    )

    assert resolution.destination is None
    assert resolution.problem is not None and "could not be registered" in resolution.problem
    assert resolution.error is not None and "cannot save" in resolution.error
    assert config.roots == []


def destination_of(config, config_path, *, mode, root_id=None, subfolder="", folder=None):
    """Shorthand for the non-registering resolution paths in tests."""
    resolution = resolve_destination(
        config,
        mode=mode,
        root_id=root_id,
        subfolder=subfolder,
        folder=folder,
        config_path=config_path,
    )
    return resolution.destination, resolution.problem


# -- removal ------------------------------------------------------------------


def _record(root_id: str, relative_path: str, state: JobState) -> ClipRecord:
    return ClipRecord(
        root_id=root_id,
        relative_path=relative_path,
        source=Fingerprint(size_bytes=10, mtime_ns=1, digest="d"),
        state=state,
    )


def test_root_job_counts_counts_non_terminal_states_only(tmp_path):
    manifest_path = tmp_path / "manifest.sqlite"
    assert root_job_counts(manifest_path, "archive") == {}

    with Manifest.open(manifest_path) as manifest:
        manifest.upsert_clip(_record("archive", "A.MP4", JobState.QUEUED))
        manifest.upsert_clip(_record("archive", "B.MP4", JobState.ENCODING))
        manifest.upsert_clip(_record("archive", "C.MP4", JobState.COMPLETE))
        manifest.upsert_clip(_record("archive", "D.MP4", JobState.REVIEW))
        manifest.upsert_clip(_record("other", "E.MP4", JobState.QUEUED))

    assert root_job_counts(manifest_path, "archive") == {"queued": 1, "encoding": 1}
    assert JobState.COMPLETE not in NON_TERMINAL_STATES
    assert JobState.FAILED not in NON_TERMINAL_STATES
    assert JobState.REVIEW not in NON_TERMINAL_STATES


def test_root_job_counts_reports_unreadable_and_unknown_manifests(tmp_path):
    assert root_job_counts(None, "archive") is None

    broken = tmp_path / "broken.sqlite"
    broken.write_text("not a database", encoding="utf-8")
    assert root_job_counts(broken, "archive") is None


def test_remove_root_pops_by_id_and_preserves_order(tmp_path):
    config = AppConfig()
    for root_id in ("a", "b", "c"):
        folder = tmp_path / root_id
        folder.mkdir()
        register_root(config, root_id=root_id, name=root_id, path=folder)

    removed = remove_root(config, "b")

    assert removed.id == "b"
    assert [root.id for root in config.roots] == ["a", "c"]
    with pytest.raises(ValueError):
        remove_root(config, "ghost")


# -- notes --------------------------------------------------------------------


def test_registration_note_names_roots_and_errors(tmp_path):
    assert registration_note(RootRegistrationOutcome()) is None
    root = make_root(tmp_path, "ride-abc12345")

    note = registration_note(RootRegistrationOutcome(registered=(root,)))
    assert note is not None and "ride-abc12345" in note and "registered new root" in note

    plural = registration_note(RootRegistrationOutcome(registered=(root, root)))
    assert plural == "registered 2 new roots"

    assert registration_note(RootRegistrationOutcome(error="boom")) == "boom"


def test_used_root_ids_covers_files_and_destination(tmp_path):
    entries = (
        SelectionEntry(tmp_path / "A.MP4", 1, "archive", "A.MP4"),
        SelectionEntry(tmp_path / "B.MP4", 1, None, None, problem="x"),
    )
    destination = Destination(DestinationKind.OUTPUT_ROOT, "compressed", "")

    assert used_root_ids(entries, destination) == ("archive", "compressed")


def test_missing_node_roots_and_warning(tmp_path):
    nodes = (
        NodeEntry(id="homeserver", name="Linux node", roots={"archive": "/srv/archive"}),
        NodeEntry(id="laptop", name="Laptop", roots={"archive": "/mnt/a", "ride": "/mnt/ride"}),
    )

    missing = missing_node_roots(nodes, ["archive", "ride"])

    assert missing == (("Linux node", ("ride",)),)
    note = format_node_warning(missing)
    assert note is not None
    assert "Linux node" in note and "ride" in note and "vc node install" in note
    assert format_node_warning(()) is None
