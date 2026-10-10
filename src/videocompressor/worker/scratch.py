"""Local scratch layout and helpers for the worker pipeline (issue #6).

Every job gets a private tree under ``<scratch_dir>/jobs/<job_id>/`` holding the
source copy, the encoded output, and the pass logs. The whole tree is discarded
when the job settles, and the whole ``jobs/`` directory is swept at startup, so
an interrupted attempt is never resumed: the clip restarts from the beginning
(plan.md resume semantics).
"""

from __future__ import annotations

import logging
import shutil
import uuid
from pathlib import Path
from typing import Protocol

__all__ = [
    "COPY_CHUNK_BYTES",
    "CopyStopped",
    "discard",
    "jobs_root",
    "new_job_dir",
    "new_job_id",
    "prepare_scratch_root",
]

_log = logging.getLogger("videocompressor.worker")

JOBS_DIRNAME = "jobs"

#: Read/write chunk for the interruptible copy (SMB reads tolerate large chunks).
COPY_CHUNK_BYTES = 64 * 1024 * 1024


class StopEvent(Protocol):
    """Anything with an ``is_set()`` stop flag (``threading.Event`` compatible)."""

    def is_set(self) -> bool: ...


class CopyStopped(RuntimeError):
    """Raised when a stop request interrupted a file copy."""


def jobs_root(scratch_dir: Path) -> Path:
    """Return the per-worker jobs directory inside ``scratch_dir``."""
    return scratch_dir / JOBS_DIRNAME


def prepare_scratch_root(scratch_dir: Path) -> Path:
    """Create the scratch jobs directory and sweep every leftover from earlier runs."""
    root = jobs_root(scratch_dir)
    root.mkdir(parents=True, exist_ok=True)
    for entry in sorted(root.iterdir()):
        _log.info("discarding leftover scratch entry %s", entry)
        discard(entry)
    return root


def new_job_dir(scratch_dir: Path, job_id: str) -> Path:
    """Create and return the private scratch directory for one job attempt."""
    path = jobs_root(scratch_dir) / job_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def new_job_id(worker_id: str) -> str:
    """Build a unique per-attempt job id matching the replacement artifact pattern."""
    return f"{worker_id[:24]}.{uuid.uuid4().hex[:12]}"


def discard(path: Path) -> None:
    """Remove a scratch tree or file, best effort."""
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
    except OSError:
        _log.warning("could not discard scratch path %s", path)


def copy_with_stop(
    source: Path,
    destination: Path,
    stop_event: StopEvent,
    *,
    chunk_bytes: int = COPY_CHUNK_BYTES,
) -> int:
    """Copy ``source`` to ``destination``, honouring ``stop_event`` between chunks.

    Returns the number of bytes written. Raises :class:`CopyStopped` when the
    stop event is set; the partial destination is left for the caller's cleanup.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with source.open("rb") as source_file, destination.open("wb") as destination_file:
        while True:
            if stop_event.is_set():
                raise CopyStopped(f"copy of {source} was stopped")
            chunk = source_file.read(chunk_bytes)
            if not chunk:
                break
            destination_file.write(chunk)
            written += len(chunk)
    return written
