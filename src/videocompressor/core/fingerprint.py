"""Cheap content fingerprints and full-file hashing.

A fingerprint is the scan/pre-replace identity of a clip: size, modification
time, and a digest sampled from the head and tail. It must stay cheap even on
multi-gigabyte GoPro clips. ``sha256_file`` provides the full digest used to
verify transfers in the replacement protocol (issue #5).
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from videocompressor.core.models import Fingerprint

#: Bytes sampled from the head and the tail for the cheap digest.
SAMPLE_BYTES = 1 << 20

#: Read chunk for the full-file hash.
READ_CHUNK_BYTES = 1 << 20


def fingerprint_file(path: Path) -> Fingerprint:
    """Return the cheap fingerprint of ``path`` (size, mtime, sampled digest)."""
    stat = path.stat()
    digest = _sampled_digest(path, stat.st_size)
    return Fingerprint(size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns, digest=digest)


def sha256_file(path: Path, *, chunk_size: int = READ_CHUNK_BYTES) -> str:
    """Return the full-file SHA-256 hex digest, streamed in chunks."""
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            hasher.update(chunk)
    return hasher.hexdigest()


def fingerprint_confirms(recorded: Fingerprint, current: Fingerprint) -> bool:
    """Whether ``current`` still confirms ``recorded`` for completion checks.

    Compares size and digest but not mtime: modification-time precision differs
    between SMB mounts and local filesystems, so a cross-node mtime mismatch
    must not invalidate a completed clip. The replacement protocol (issue #5)
    uses strict ``recorded == current`` for the pre-replace source check.
    """
    return recorded.size_bytes == current.size_bytes and recorded.digest == current.digest


def _sampled_digest(path: Path, size: int) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        if size <= 2 * SAMPLE_BYTES:
            hasher.update(handle.read())
        else:
            hasher.update(handle.read(SAMPLE_BYTES))
            handle.seek(-SAMPLE_BYTES, os.SEEK_END)
            hasher.update(handle.read(SAMPLE_BYTES))
    return hasher.hexdigest()
