"""GUI suite setup: offscreen Qt and a session-wide application (issue #8).

Pure-Python modules (``state``, ``scan_service``, the config additions, and the
CLI seam) run without the optional ``gui`` extra. Qt test modules call
``pytest.importorskip("PySide6")`` before importing PySide6, so CI stays green
without the extra.
"""

from __future__ import annotations

import os

import pytest

# Tests must never pop windows on a developer machine or need a display in CI.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="session")
def qapp():
    """A shared ``QApplication`` for widget tests (skips without PySide6)."""
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app
