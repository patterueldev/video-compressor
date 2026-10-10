"""Controller protocol constants and shared helpers.

The lease constants are owned by ``core/manifest.py`` (the SQLite manifest is
the source of truth for lease behavior); this module re-exports them and adds
the controller's own HTTP/sweep constants.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime

from videocompressor.core.manifest import (
    HEARTBEAT_INTERVAL_SECONDS,
    LEASE_STATES,
    LEASE_TTL_SECONDS,
)

__all__ = [
    "EVENTS_PAGE_LIMIT",
    "EVENTS_PAGE_MAX",
    "HEARTBEAT_INTERVAL_SECONDS",
    "LEASE_STATES",
    "LEASE_TTL_SECONDS",
    "MAX_BODY_BYTES",
    "SWEEP_INTERVAL_SECONDS",
    "WORKER_ID_PATTERN",
    "Clock",
    "system_now",
]

SWEEP_INTERVAL_SECONDS = 5

#: Worker ids match this pattern (``docs/architecture.md``).
WORKER_ID_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,64}")

EVENTS_PAGE_LIMIT = 200
EVENTS_PAGE_MAX = 1000
MAX_BODY_BYTES = 1_000_000

Clock = Callable[[], datetime]


def system_now() -> datetime:
    """Current time as a timezone-aware local datetime."""
    return datetime.now().astimezone()
