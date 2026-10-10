"""Fingerprints: sampled head/tail digests, full hashes, completion comparison."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from videocompressor.core.fingerprint import (
    SAMPLE_BYTES,
    fingerprint_confirms,
    fingerprint_file,
    sha256_file,
)
from videocompressor.core.models import Fingerprint


def _write_file(path: Path, size: int, seed: int = 0) -> Path:
    with path.open("wb") as handle:
        handle.write(bytes((i + seed) % 251 for i in range(min(size, 4096))))
        if size > 4096:
            handle.truncate(size)
    return path


def _flip_byte(path: Path, offset: int) -> None:
    with path.open("r+b") as handle:
        handle.seek(offset)
        byte = handle.read(1)
        handle.seek(offset)
        handle.write(bytes([byte[0] ^ 0xFF]))


def test_fingerprint_reports_size_mtime_and_stable_digest(tmp_path: Path):
    path = _write_file(tmp_path / "clip.MP4", 8192)

    first = fingerprint_file(path)
    second = fingerprint_file(path)

    assert first == second
    assert first.size_bytes == 8192
    assert first.mtime_ns == path.stat().st_mtime_ns
    assert len(first.digest) == 64


def test_mtime_change_keeps_the_content_digest(tmp_path: Path):
    path = _write_file(tmp_path / "clip.MP4", 8192)
    before = fingerprint_file(path)

    os.utime(path, ns=(before.mtime_ns + 10**9, before.mtime_ns + 10**9))
    after = fingerprint_file(path)

    assert after.mtime_ns != before.mtime_ns
    assert after.digest == before.digest


def test_head_changes_are_detected_on_small_files(tmp_path: Path):
    path = _write_file(tmp_path / "clip.MOV", 4096)
    before = fingerprint_file(path)

    _flip_byte(path, 3)

    assert fingerprint_file(path).digest != before.digest


def test_head_and_tail_changes_are_detected_on_large_files(tmp_path: Path):
    path = _write_file(tmp_path / "clip.MP4", 3 * SAMPLE_BYTES)
    before = fingerprint_file(path)

    _flip_byte(path, 5)
    head_changed = fingerprint_file(path)
    assert head_changed.digest != before.digest

    path = _write_file(tmp_path / "other.MP4", 3 * SAMPLE_BYTES)
    before = fingerprint_file(path)
    _flip_byte(path, path.stat().st_size - 5)
    assert fingerprint_file(path).digest != before.digest


def test_sha256_file_matches_whole_file_hashing(tmp_path: Path):
    path = _write_file(tmp_path / "clip.MP4", 2 * SAMPLE_BYTES + 17)

    assert sha256_file(path, chunk_size=12345) == hashlib.sha256(path.read_bytes()).hexdigest()


def test_fingerprint_confirms_ignores_mtime_but_not_content(tmp_path: Path):
    path = _write_file(tmp_path / "clip.MP4", 8192)
    recorded = fingerprint_file(path)

    assert fingerprint_confirms(recorded, fingerprint_file(path))
    ignored_mtime = Fingerprint(size_bytes=8192, mtime_ns=0, digest=recorded.digest)
    assert fingerprint_confirms(recorded, ignored_mtime)
    assert not fingerprint_confirms(
        recorded, Fingerprint(size_bytes=8193, mtime_ns=recorded.mtime_ns, digest=recorded.digest)
    )
    assert not fingerprint_confirms(
        recorded, Fingerprint(size_bytes=8192, mtime_ns=recorded.mtime_ns, digest="0" * 64)
    )
