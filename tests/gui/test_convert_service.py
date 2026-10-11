"""Convert-flow selection and payload logic (issue #41).

Pure Python: runs in CI without the optional ``gui`` extra.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from videocompressor.core.models import ArchiveRoot, Destination, DestinationKind
from videocompressor.gui.convert_service import (
    MODE_CUSTOM_FOLDER,
    MODE_OUTPUT_ROOT,
    MODE_SAME_FOLDER,
    REASON_LABELS,
    ConvertSelection,
    build_enqueue_payload,
    destination_from_fields,
    effective_delete_original,
    status_text,
)


def entry_file(root: Path, relative: str, payload: bytes = b"x" * 100) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def make_root(path: Path, root_id: str = "archive") -> ArchiveRoot:
    return ArchiveRoot(id=root_id, name=root_id, path=path)


def make_selection(*roots: ArchiveRoot) -> ConvertSelection:
    return ConvertSelection(lambda: list(roots))


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    root = tmp_path / "archive"
    root.mkdir()
    return root


# -- selection ---------------------------------------------------------------


def test_add_paths_recurses_and_collects_only_videos(archive):
    entry_file(archive, "IMPORTS/GOPRO-20261010/GH011016.MP4", b"a" * 300)
    entry_file(archive, "RIDE/GS010001.mov", b"b" * 100)
    entry_file(archive, "RIDE/notes.txt")
    entry_file(archive, "RIDE/thumb.JPG", b"j")
    selection = make_selection(make_root(archive))

    outcome = selection.add_paths([archive])

    assert (outcome.added, outcome.ignored_non_video, outcome.duplicates) == (2, 2, 0)
    entries = selection.entries()
    assert [entry.relative_path for entry in entries] == [
        "IMPORTS/GOPRO-20261010/GH011016.MP4",
        "RIDE/GS010001.mov",
    ]
    first = entries[0]
    assert first.ready
    assert first.root_id == "archive"
    assert first.size_bytes == 300
    assert first.problem is None


def test_add_paths_appends_and_dedupes(archive):
    first = entry_file(archive, "RIDE/GS010001.MP4", b"a" * 100)
    entry_file(archive, "RIDE/GS010002.MP4", b"b" * 50)
    selection = make_selection(make_root(archive))

    assert selection.add_paths([first]).added == 1
    outcome = selection.add_paths([archive])

    assert (outcome.added, outcome.duplicates) == (1, 1)
    assert len(selection.entries()) == 2
    assert selection.total_bytes() == 150


def test_non_video_explicit_file_is_ignored(archive):
    note = entry_file(archive, "notes.txt")
    selection = make_selection(make_root(archive))

    outcome = selection.add_paths([note])

    assert outcome.added == 0
    assert outcome.ignored_non_video == 1
    assert selection.entries() == ()


def test_missing_path_becomes_a_problem_row(archive):
    selection = make_selection(make_root(archive))

    outcome = selection.add_paths([archive / "gone.MP4"])

    assert outcome.added == 1
    (row,) = selection.entries()
    assert not row.ready
    assert "no longer exists" in row.problem


def test_file_outside_roots_is_a_problem_row(tmp_path, archive):
    outside = entry_file(tmp_path / "elsewhere", "clip.MP4")
    selection = make_selection(make_root(archive))

    selection.add_paths([outside])

    (row,) = selection.entries()
    assert not row.ready
    assert "registered root" in row.problem
    assert selection.ready_entries() == ()


def test_most_specific_root_wins(tmp_path, archive):
    imports = archive / "IMPORTS"
    imports.mkdir()
    clip = entry_file(imports, "GOPRO/GH011016.MP4")
    selection = make_selection(make_root(archive), make_root(imports, "imports"))

    selection.add_paths([clip])

    (row,) = selection.entries()
    assert (row.root_id, row.relative_path) == ("imports", "GOPRO/GH011016.MP4")


def test_symlinks_are_skipped(archive):
    real = entry_file(archive, "RIDE/GS010001.MP4")
    (archive / "RIDE" / "alias.MP4").symlink_to(real)
    selection = make_selection(make_root(archive))

    outcome = selection.add_paths([archive])

    assert outcome.added == 1
    assert outcome.ignored_non_video == 1  # the symlinked alias
    assert [entry.relative_path for entry in selection.entries()] == ["RIDE/GS010001.MP4"]


def test_clear_resets_the_selection(archive):
    clip = entry_file(archive, "RIDE/GS010001.MP4")
    selection = make_selection(make_root(archive))
    selection.add_paths([clip])
    assert selection.total_bytes() == 100

    selection.clear()

    assert selection.entries() == ()
    assert selection.total_bytes() == 0
    assert selection.add_paths([clip]).added == 1  # the key set was cleared too


# -- destinations ------------------------------------------------------------


def test_destination_output_root(archive):
    roots = [make_root(archive, "compressed")]

    destination, problem = destination_from_fields(
        mode=MODE_OUTPUT_ROOT, root_id="compressed", subfolder="", folder=None, roots=roots
    )
    assert problem is None
    assert destination == Destination(DestinationKind.OUTPUT_ROOT, "compressed", "")

    destination, problem = destination_from_fields(
        mode=MODE_OUTPUT_ROOT, root_id="compressed", subfolder=" 2026-10 ", folder=None,
        roots=roots,
    )
    assert problem is None
    assert destination.relative_path == "2026-10"


def test_destination_output_root_rejects_unknown_root_and_unsafe_subfolder(archive):
    roots = [make_root(archive, "compressed")]

    destination, problem = destination_from_fields(
        mode=MODE_OUTPUT_ROOT, root_id="ghost", subfolder="", folder=None, roots=roots
    )
    assert destination is None
    assert "registered output root" in problem

    for subfolder in ("../escape", "/absolute", "a\\b"):
        destination, problem = destination_from_fields(
            mode=MODE_OUTPUT_ROOT, root_id="compressed", subfolder=subfolder,
            folder=None, roots=roots,
        )
        assert destination is None
        assert "subfolder" in problem


def test_destination_custom_folder(tmp_path, archive):
    roots = [make_root(archive)]

    destination, problem = destination_from_fields(
        mode=MODE_CUSTOM_FOLDER, root_id=None, subfolder="",
        folder=archive / "Out" / "2026-10", roots=roots,
    )
    assert problem is None
    assert destination == Destination(DestinationKind.OUTPUT_ROOT, "archive", "Out/2026-10")

    destination, problem = destination_from_fields(
        mode=MODE_CUSTOM_FOLDER, root_id=None, subfolder="", folder=archive, roots=roots
    )
    assert destination is None
    assert "subfolder" in problem

    outside = tmp_path / "out"
    outside.mkdir()
    destination, problem = destination_from_fields(
        mode=MODE_CUSTOM_FOLDER, root_id=None, subfolder="", folder=outside, roots=roots
    )
    assert destination is None
    assert "registered root" in problem

    destination, problem = destination_from_fields(
        mode=MODE_CUSTOM_FOLDER, root_id=None, subfolder="", folder=None, roots=roots
    )
    assert destination is None
    assert "output folder" in problem


def test_destination_same_folder():
    destination, problem = destination_from_fields(
        mode=MODE_SAME_FOLDER, root_id=None, subfolder="", folder=None, roots=[]
    )
    assert problem is None
    assert destination == Destination(DestinationKind.SAME_FOLDER)


def test_same_folder_forces_delete_original():
    assert effective_delete_original(MODE_SAME_FOLDER, False) is True
    assert effective_delete_original(MODE_SAME_FOLDER, True) is True
    assert effective_delete_original(MODE_OUTPUT_ROOT, False) is False
    assert effective_delete_original(MODE_OUTPUT_ROOT, True) is True


# -- payload and result mapping ----------------------------------------------


def test_build_enqueue_payload_shape(archive):
    clip = entry_file(archive, "RIDE/GS010001.MP4", b"x" * 50)
    selection = make_selection(make_root(archive))
    selection.add_paths([clip, archive / "gone.MP4"])
    destination, _problem = destination_from_fields(
        mode=MODE_OUTPUT_ROOT, root_id="compressed", subfolder="",
        folder=None, roots=[make_root(archive, "compressed")],
    )

    payload = build_enqueue_payload(
        selection.ready_entries(), destination, template_id="motorcycle", delete_original=False
    )

    assert payload == {
        "files": [{"root_id": "archive", "relative_path": "RIDE/GS010001.MP4"}],
        "destination": {"kind": "output_root", "root_id": "compressed"},
        "template_id": "motorcycle",
        "delete_original": False,
    }


def test_same_folder_payload_always_deletes(archive):
    clip = entry_file(archive, "RIDE/GS010001.MP4")
    selection = make_selection(make_root(archive))
    selection.add_paths([clip])
    destination, _problem = destination_from_fields(
        mode=MODE_SAME_FOLDER, root_id=None, subfolder="", folder=None, roots=[]
    )

    payload = build_enqueue_payload(
        selection.ready_entries(),
        destination,
        template_id="motorcycle",
        delete_original=effective_delete_original(MODE_SAME_FOLDER, False),
    )

    assert payload["destination"] == {"kind": "same_folder"}
    assert payload["delete_original"] is True


def test_apply_enqueue_result_maps_statuses_and_reasons(archive):
    entry_file(archive, "A.MP4")
    entry_file(archive, "B.MP4")
    entry_file(archive, "C.MP4")
    selection = make_selection(make_root(archive))
    selection.add_paths([archive / "A.MP4", archive / "B.MP4", archive / "C.MP4"])
    body = {
        "jobs": [
            {"root_id": "archive", "relative_path": "A.MP4", "status": "queued", "reason": None},
            {
                "root_id": "archive",
                "relative_path": "B.MP4",
                "status": "skipped",
                "reason": "already_complete",
            },
            {
                "root_id": "archive",
                "relative_path": "C.MP4",
                "status": "rejected",
                "reason": "unreadable",
            },
        ],
        "queued": 1,
        "skipped": 1,
        "rejected": 1,
    }

    outcome = selection.apply_enqueue_result(body)

    assert (outcome.queued, outcome.skipped, outcome.rejected, outcome.total) == (1, 1, 1, 3)
    assert [entry.status for entry in selection.entries()] == [
        "queued",
        "skipped: already complete",
        "rejected: unreadable",
    ]


def test_apply_enqueue_result_tolerates_malformed_payload(archive):
    entry_file(archive, "A.MP4")
    selection = make_selection(make_root(archive))
    selection.add_paths([archive / "A.MP4"])

    outcome = selection.apply_enqueue_result({})

    assert outcome.total == 0
    assert selection.entries()[0].status is None

    selection.apply_enqueue_result(
        {"jobs": [None, {"root_id": 1}, "x", {"root_id": "archive"}], "queued": True}
    )
    assert selection.entries()[0].status is None


def test_status_text_labels():
    assert status_text("queued", None) == "queued"
    assert status_text("skipped", "needs_review") == "skipped: needs review"
    assert status_text("rejected", "weird_slug") == "rejected: weird_slug"
    assert status_text("rejected", None) == "rejected: unknown reason"
    assert set(REASON_LABELS) == {
        "already_complete",
        "already_queued",
        "needs_review",
        "previously_failed",
        "duplicate_in_batch",
        "unknown_root",
        "unsafe_relative_path",
        "not_a_file",
        "unreadable",
    }
