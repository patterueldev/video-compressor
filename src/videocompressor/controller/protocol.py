"""HTTP-mappable controller errors and JSON payload helpers.

The validators, coercers, and converters here are shared by the HTTP layer
(``controller/http.py``) and the ``ControllerServer`` facade
(``controller/server.py``); they keep the controller's request/response
coercion rules in one place.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from videocompressor.controller.store import EventRow, RunRow

__all__ = ["ControllerError"]


class ControllerError(Exception):
    """An HTTP-mappable controller error (status, slug, and human message)."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _unknown_worker(worker_id: str) -> ControllerError:
    return ControllerError(404, "unknown_worker", f"worker {worker_id!r} is not registered")


def _require_str(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ControllerError(400, "bad_request", f"{key!r} must be a non-empty string")
    return value


def _optional_str(
    payload: Mapping[str, Any], key: str, *, default: str | None = None
) -> str | None:
    value = payload.get(key, default)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ControllerError(400, "bad_request", f"{key!r} must be a string")
    return value


def _require_int(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ControllerError(400, "bad_request", f"{key!r} must be an integer")
    return value


def _optional_int(payload: Mapping[str, Any], key: str, *, default: int) -> int:
    value = payload.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ControllerError(400, "bad_request", f"{key!r} must be an integer")
    return value


def _optional_str_map(payload: Mapping[str, Any], key: str) -> dict[str, str]:
    value = payload.get(key)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ControllerError(400, "bad_request", f"{key!r} must be an object")
    out: dict[str, str] = {}
    for map_key, map_value in value.items():
        if not isinstance(map_key, str) or not isinstance(map_value, str):
            raise ControllerError(400, "bad_request", f"{key!r} must map strings to strings")
        out[map_key] = map_value
    return out


def _settings_hash(settings: Mapping[str, Any]) -> str:
    canonical = json.dumps(settings, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _event_dict(event: EventRow) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "seq": event.seq,
        "at": event.at.isoformat(),
        "level": event.level,
        "message": event.message,
    }
    if event.worker_id is not None:
        payload["worker_id"] = event.worker_id
    if event.root_id is not None:
        payload["root_id"] = event.root_id
    if event.relative_path is not None:
        payload["relative_path"] = event.relative_path
    if event.run_id is not None:
        payload["run_id"] = event.run_id
    return payload


def _run_dict(run: RunRow) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "started_at": run.started_at.isoformat(),
        "ended_at": run.ended_at.isoformat() if run.ended_at else None,
        "completed": run.completed,
        "skipped": run.skipped,
        "failed": run.failed,
        "review": run.review,
    }
