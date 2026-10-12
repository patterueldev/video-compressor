"""Fixtures for the worker service suites (issue #51).

Local to ``tests/worker`` (like ``tests/gui/conftest.py``); the shared
``tests/conftest.py`` registrar stays untouched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from videocompressor.worker.service import ServicePaths
from worker.service_support import RecordingRunner, make_paths, write_worker_config


@pytest.fixture
def runner() -> RecordingRunner:
    return RecordingRunner()


@pytest.fixture
def paths_for(tmp_path: Path):
    """Factory for injectable service locations: ``paths_for("macos" | "linux")``."""

    def _make(platform: str) -> ServicePaths:
        return make_paths(tmp_path, platform)

    return _make


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    return write_worker_config(tmp_path)
