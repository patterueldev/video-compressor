"""Engine/media fixtures (issues #2, #3, #5)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from videocompressor.core.models import Fingerprint

FIXTURE_DIR = Path(__file__).parent / "ffprobe"
_CHUNK_SIZE = 1 << 20


@pytest.fixture
def ffprobe_fixture() -> Callable[[str], dict[str, Any]]:
    """Return a loader for recorded ffprobe JSON payloads."""

    def load(name: str) -> dict[str, Any]:
        return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))

    return load


@pytest.fixture
def fingerprint_full() -> Callable[[Path], Fingerprint]:
    """Stand-in for issue #3's sampled fingerprint: size + mtime + full SHA-256."""

    def _fingerprint(path: Path) -> Fingerprint:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(_CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
        return Fingerprint(
            size_bytes=size,
            mtime_ns=path.stat().st_mtime_ns,
            digest=digest.hexdigest(),
        )

    return _fingerprint
