"""Typed HTTP client for controller contract v1 behind the GUI monitor (issue #9).

The monitor polls the same endpoints workers use (``GET /workers``, ``GET
/events``, ``GET /summary``, ``POST .../stop|resume``), whether the GUI hosts
the controller in-process or attaches to one that is already running. Pure
Python: no PySide6, so the client runs in CI without the optional ``gui`` extra.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from typing import Any

from videocompressor.controller.protocol import ControllerError

__all__ = ["HttpControllerClient", "MonitorUnavailable", "describe_monitor_error"]

API_PREFIX = "/api/v1"


class MonitorUnavailable(RuntimeError):
    """The controller could not be reached or answered with an invalid response."""


class HttpControllerClient:
    """Minimal typed client for the monitor endpoints of controller contract v1."""

    def __init__(self, base_url: str, token: str | None, *, timeout: float = 2.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    # -- API ---------------------------------------------------------------

    def workers(self) -> dict[str, Any]:
        """``GET /workers``: the registry snapshot with progress."""
        return self._request("GET", "/workers") or {}

    def events(self, *, after: int | None = None, limit: int | None = None) -> dict[str, Any]:
        """``GET /events``: one bounded run-log page, oldest first."""
        query: dict[str, str] = {}
        if after is not None:
            query["after"] = str(int(after))
        if limit is not None:
            query["limit"] = str(int(limit))
        return self._request("GET", _with_query("/events", query)) or {}

    def summary(self, *, run_id: str | None = None) -> dict[str, Any]:
        """``GET /summary``: live counts, or a finished run's counts."""
        query = {"run_id": run_id} if run_id is not None else {}
        return self._request("GET", _with_query("/summary", query)) or {}

    def stop_worker(self, worker_id: str) -> None:
        """``POST /workers/{id}/stop``: ask the worker to terminate its active job."""
        self._request("POST", f"/workers/{_quote(worker_id)}/stop", {})

    def resume_worker(self, worker_id: str) -> None:
        """``POST /workers/{id}/resume``: clear a pending stop request."""
        self._request("POST", f"/workers/{_quote(worker_id)}/resume", {})

    def probe(self) -> None:
        """Check that a controller v1 endpoint answers; raises on failure."""
        self.workers()

    # -- plumbing ----------------------------------------------------------

    def _request(
        self, method: str, path: str, payload: Mapping[str, Any] | None = None
    ) -> dict[str, Any] | None:
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + API_PREFIX + path, data=data, method=method
        )
        if self._token is not None:
            request.add_header("Authorization", f"Bearer {self._token}")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise _error_from_http(exc) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise MonitorUnavailable(
                f"controller unreachable at {self.base_url}: {exc}"
            ) from exc
        if not raw:
            return None
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            raise MonitorUnavailable(f"invalid response from {self.base_url}") from None
        if not isinstance(body, dict):
            raise MonitorUnavailable(f"invalid response from {self.base_url}")
        return body


def describe_monitor_error(error: BaseException) -> str:
    """One-line message for the monitor's status line."""
    if isinstance(error, ControllerError):
        return error.message
    if isinstance(error, MonitorUnavailable):
        return str(error)
    return f"unexpected error: {error!r}"


def _error_from_http(exc: urllib.error.HTTPError) -> ControllerError:
    code = "http_error"
    message = str(exc.reason or "")
    try:
        payload = json.loads(exc.read())
    except (json.JSONDecodeError, ValueError):
        payload = None
    if isinstance(payload, Mapping):
        error = payload.get("error")
        if isinstance(error, Mapping):
            code = _optional_str(error.get("code")) or code
            message = _optional_str(error.get("message")) or message
    return ControllerError(exc.code, code, message)


def _with_query(path: str, query: Mapping[str, str]) -> str:
    if not query:
        return path
    return f"{path}?{urllib.parse.urlencode(query)}"


def _quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None
