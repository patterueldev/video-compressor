"""Stop-marker persistence for the app-managed local worker (issue #52, #70).

Pure Python. ``stop()``/``shutdown()`` and the issue #70 startup sweep record
the time they confirmed no app-managed worker runs; a registry row whose last
heartbeat predates that time is a dead registration, not a live node, so a
relaunch can start the local worker without waiting out the lease window.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from videocompressor.gui.local_worker_config import STATE_FILENAME

__all__ = ["marker_path", "read_stopped_at", "stopped_before", "write_stop_marker"]

_log = logging.getLogger("videocompressor.gui")


def marker_path(config_path: Path) -> Path:
    """The stop marker next to the per-user config."""
    return config_path.parent / STATE_FILENAME


def read_stopped_at(config_path: Path) -> datetime | None:
    """The recorded stop time, or ``None`` when the marker is missing/invalid."""
    try:
        payload = json.loads(marker_path(config_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    value = payload.get("stopped_at")
    if not isinstance(value, str):
        return None
    try:
        return _as_utc(datetime.fromisoformat(value))
    except ValueError:
        return None


def write_stop_marker(config_path: Path) -> None:
    """Record now as a confirmed stop; best effort, never raises."""
    payload = {"stopped_at": datetime.now(UTC).isoformat()}
    try:
        marker_path(config_path).write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        _log.warning("could not record the local worker stop time")


def stopped_before(last_seen: datetime | None, stopped_at: datetime) -> bool:
    """Whether ``last_seen`` predates a confirmed stop (a dead registration)."""
    if last_seen is None:
        return False
    return _as_utc(last_seen) <= stopped_at


def _as_utc(when: datetime) -> datetime:
    return when.replace(tzinfo=UTC) if when.tzinfo is None else when
