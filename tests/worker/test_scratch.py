"""Scratch layout, startup sweep, and interruptible copy (issue #6)."""

from __future__ import annotations

import re
import threading
from pathlib import Path

import pytest

from videocompressor.worker.scratch import (
    CopyStopped,
    copy_with_stop,
    discard,
    jobs_root,
    new_job_dir,
    new_job_id,
    prepare_scratch_root,
)


def test_prepare_scratch_root_sweeps_leftovers(tmp_path: Path):
    scratch = tmp_path / "scratch"
    stale = scratch / "jobs" / "old-job"
    stale.mkdir(parents=True)
    (stale / "source.mp4").write_bytes(b"stale")
    (scratch / "jobs" / "loose-file").write_bytes(b"x")

    root = prepare_scratch_root(scratch)

    assert root == jobs_root(scratch) == scratch / "jobs"
    assert list(root.iterdir()) == []


def test_new_job_id_matches_the_artifact_pattern():
    job_id = new_job_id("node-a")
    assert job_id.startswith("node-a.")
    assert re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", job_id)
    # long worker ids are truncated so the id stays inside the 64-char pattern
    assert len(new_job_id("w" * 80)) <= 64


def test_new_job_dir_and_discard_are_idempotent(tmp_path: Path):
    scratch = tmp_path / "scratch"
    path = new_job_dir(scratch, new_job_id("node-a"))
    (path / "source").mkdir()
    (path / "source" / "clip.mp4").write_bytes(b"data")

    discard(path)
    assert not path.exists()
    discard(path)  # safe on a missing path


def test_copy_with_stop_copies_all_bytes(tmp_path: Path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"a" * 1000)
    destination = tmp_path / "nested" / "dest.bin"

    written = copy_with_stop(source, destination, threading.Event(), chunk_bytes=64)

    assert written == 1000
    assert destination.read_bytes() == source.read_bytes()


def test_copy_with_stop_aborts_between_chunks(tmp_path: Path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"a" * 1000)
    destination = tmp_path / "dest.bin"
    stop = threading.Event()
    stop.set()

    with pytest.raises(CopyStopped):
        copy_with_stop(source, destination, stop, chunk_bytes=64)
    assert destination.read_bytes() == b""
