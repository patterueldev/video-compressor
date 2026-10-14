"""HTTP client for controller contract v1 (docs/architecture.md; issue #7 server).

The worker speaks JSON over ``urllib.request`` with a bearer token. Failures are
typed so the agent can tell apart "controller is away" (retry), "token is wrong"
(fatal), "worker is unknown" (re-register), and "lease is gone" (abort the job).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from videocompressor.core.models import Fingerprint

__all__ = [
    "ClaimedJob",
    "ControllerAuthError",
    "ControllerClient",
    "ControllerError",
    "ControllerUnavailable",
    "LeaseConflict",
]

API_PREFIX = "/api/v1"


class ControllerError(RuntimeError):
    """An HTTP error response from the controller."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"controller error {status} ({code}): {message}")
        self.status = status
        self.code = code
        self.message = message


class LeaseConflict(ControllerError):
    """The worker no longer holds the lease it asked about (HTTP 409)."""


class ControllerAuthError(ControllerError):
    """The bearer token was rejected (HTTP 401); a wrong token cannot self-heal."""


class ControllerUnavailable(RuntimeError):
    """The controller could not be reached (transport failure or timeout)."""


@dataclass(frozen=True)
class ClaimedJob:
    """One job as delivered by ``POST /jobs/claim``.

    ``destination`` is the raw convert-flow destination spec (issue #40; decoded
    by :mod:`videocompressor.worker.destination_paths`), ``None`` for a legacy
    claim. ``delete_original`` is ``None`` when the claim carried no flag (legacy
    or an omitted default), and explicit ``True``/``False`` otherwise. ``naming``
    is the raw optional output-name spec (issue #87), ``None`` when the claim
    omitted it or carried a malformed value.
    """

    root_id: str
    relative_path: str
    template_id: str | None = None
    settings: Mapping[str, Any] = field(default_factory=dict)
    settings_hash: str | None = None
    capture_time: str | None = None
    destination: Mapping[str, Any] | None = None
    delete_original: bool | None = None
    naming: Mapping[str, Any] | None = None


class ControllerClient:
    """Minimal typed client for the controller's HTTP contract v1."""

    def __init__(self, base_url: str, token: str, *, timeout_s: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._timeout_s = timeout_s

    # -- API ---------------------------------------------------------------

    def register(
        self,
        *,
        worker_id: str,
        name: str,
        roots: Mapping[str, str],
        scratch_dir: str,
        thread_cap: int,
        version: str,
        machine_id: str | None = None,
    ) -> dict[str, Any]:
        """Register (or refresh) this node; returns the lease/heartbeat intervals."""
        body = self._request(
            "POST",
            "/workers/register",
            {
                "worker_id": worker_id,
                "name": name,
                "roots": dict(roots),
                "scratch_dir": scratch_dir,
                "thread_cap": thread_cap,
                "version": version,
                "machine_id": machine_id,
            },
        )
        return body or {}

    def heartbeat(
        self,
        worker_id: str,
        *,
        active: tuple[str, str] | None = None,
        progress: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Renew this worker's lease and, when ``active`` is set, its job lease."""
        payload: dict[str, Any] = {}
        if active is not None:
            payload["active"] = {"root_id": active[0], "relative_path": active[1]}
        if progress is not None:
            payload["progress"] = dict(progress)
        body = self._request("POST", f"/workers/{worker_id}/heartbeat", payload)
        return body or {}

    def claim(self, worker_id: str) -> ClaimedJob | None:
        """Claim the next queued clip; ``None`` when nothing is available."""
        body = self._request("POST", "/jobs/claim", {"worker_id": worker_id})
        if body is None:
            return None
        payload = body.get("job")
        if not isinstance(payload, Mapping):
            raise ControllerError(200, "bad_response", "claim response carried no job")
        root_id = payload.get("root_id")
        relative_path = payload.get("relative_path")
        if not isinstance(root_id, str) or not isinstance(relative_path, str):
            raise ControllerError(200, "bad_response", "claim response carried a malformed job")
        settings = payload.get("settings")
        destination = payload.get("destination")
        delete_original = payload.get("delete_original")
        naming = payload.get("naming")
        return ClaimedJob(
            root_id=root_id,
            relative_path=relative_path,
            template_id=_optional_str(payload.get("template_id")),
            settings=dict(settings) if isinstance(settings, Mapping) else {},
            settings_hash=_optional_str(payload.get("settings_hash")),
            capture_time=_optional_str(payload.get("capture_time")),
            destination=dict(destination) if isinstance(destination, Mapping) else None,
            delete_original=delete_original if isinstance(delete_original, bool) else None,
            naming=dict(naming) if isinstance(naming, Mapping) else None,
        )

    def complete(self, worker_id: str, job: ClaimedJob, output: Fingerprint) -> None:
        """Mark the held clip complete with its output fingerprint."""
        self._request(
            "POST",
            "/jobs/complete",
            {
                "worker_id": worker_id,
                "root_id": job.root_id,
                "relative_path": job.relative_path,
                "output": {
                    "size_bytes": output.size_bytes,
                    "mtime_ns": output.mtime_ns,
                    "digest": output.digest,
                },
            },
        )

    def fail(
        self, worker_id: str, job: ClaimedJob, *, error: str, disposition: str
    ) -> None:
        """Report a terminal failure, a review hand-off, or an operator-stop requeue."""
        self._request(
            "POST",
            "/jobs/fail",
            {
                "worker_id": worker_id,
                "root_id": job.root_id,
                "relative_path": job.relative_path,
                "error": error,
                "disposition": disposition,
            },
        )

    # -- plumbing ----------------------------------------------------------

    def _request(
        self, method: str, path: str, payload: Mapping[str, Any] | None
    ) -> dict[str, Any] | None:
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + API_PREFIX + path, data=data, method=method
        )
        request.add_header("Authorization", f"Bearer {self._token}")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_s) as response:
                raw = response.read()
                if not raw:
                    return None
                body = json.loads(raw)
                if not isinstance(body, dict):
                    raise ControllerError(
                        response.status, "bad_response", "controller returned a non-object body"
                    )
                return body
        except urllib.error.HTTPError as exc:
            raise self._error_from_http(exc) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ControllerUnavailable(
                f"controller unreachable at {self.base_url}: {exc}"
            ) from exc

    def _error_from_http(self, exc: urllib.error.HTTPError) -> ControllerError:
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
        error_type = {401: ControllerAuthError, 409: LeaseConflict}.get(exc.code, ControllerError)
        return error_type(exc.code, code, message)


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None
