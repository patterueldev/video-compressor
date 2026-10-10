"""Controller protocol constants and shared helpers.

The v1 contract fixes the lease and heartbeat intervals (``docs/architecture.md``):
a claim starts a 90 s lease and workers renew every 15 s.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime

from videocompressor.core.models import JobState

LEASE_TTL_SECONDS = 90
HEARTBEAT_INTERVAL_SECONDS = 15
SWEEP_INTERVAL_SECONDS = 5

#: Jobs in these states are held by a worker under a live (or expired) lease.
ACTIVE_STATES = frozenset(
    {
        JobState.CLAIMED,
        JobState.COPIED,
        JobState.ENCODING,
        JobState.VALIDATED,
        JobState.TRANSFERRING,
        JobState.REPLACING,
    }
)

#: Worker ids match this pattern (``docs/architecture.md``).
WORKER_ID_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,64}")

EVENTS_PAGE_LIMIT = 200
EVENTS_PAGE_MAX = 1000
MAX_BODY_BYTES = 1_000_000

Clock = Callable[[], datetime]


def system_now() -> datetime:
    """Current time as a timezone-aware local datetime."""
    return datetime.now().astimezone()
