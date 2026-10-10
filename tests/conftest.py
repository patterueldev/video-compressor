"""Shared helpers for fixture-based core tests (issues #2/#3)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "ffprobe"


@pytest.fixture
def ffprobe_fixture() -> Callable[[str], dict[str, Any]]:
    """Return a loader for recorded ffprobe JSON payloads."""

    def load(name: str) -> dict[str, Any]:
        return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))

    return load
