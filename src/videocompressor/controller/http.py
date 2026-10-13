"""HTTP plumbing for the controller: routing, auth, and JSON bodies.

The handler delegates to in-process methods on
:class:`videocompressor.controller.server.ControllerServer`, so the GUI can
host the controller and call it directly while worker nodes use HTTP.
"""

from __future__ import annotations

import contextlib
import hmac
import json
import logging
import socket
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, unquote, urlsplit

from videocompressor.controller.constants import EVENTS_PAGE_LIMIT, MAX_BODY_BYTES
from videocompressor.controller.protocol import ControllerError, _require_str, _stop_mode

if TYPE_CHECKING:
    from videocompressor.controller.server import ControllerServer

_log = logging.getLogger("videocompressor.controller")

_WORKER_ID_SEGMENT = object()

_ROUTES: tuple[tuple[str, tuple[object, ...], str], ...] = (
    ("POST", ("workers", "register"), "register"),
    ("POST", ("workers", _WORKER_ID_SEGMENT, "heartbeat"), "heartbeat"),
    ("POST", ("workers", _WORKER_ID_SEGMENT, "stop"), "stop"),
    ("POST", ("workers", _WORKER_ID_SEGMENT, "resume"), "resume"),
    ("POST", ("jobs",), "enqueue"),
    ("POST", ("jobs", "claim"), "claim"),
    ("POST", ("jobs", "complete"), "complete"),
    ("POST", ("jobs", "fail"), "fail"),
    ("GET", ("events",), "events"),
    ("GET", ("workers",), "workers"),
    ("GET", ("summary",), "summary"),
)


class _ControllerHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    # socketserver defaults the listen backlog to 5; a burst of reconnecting
    # workers overflows that accept queue, and macOS resets the overflowing
    # connection (ConnectionResetError in the client's connect(), issue #24).
    # SOMAXCONN is the OS accept-queue limit.
    request_queue_size = socket.SOMAXCONN

    def __init__(self, address: tuple[str, int], controller: ControllerServer) -> None:
        super().__init__(address, _RequestHandler)
        self.controller = controller


class _RequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _ControllerHTTPServer

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._dispatch("POST")

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - API name
        _log.debug("%s - %s", self.address_string(), format % args)

    # -- plumbing ----------------------------------------------------------

    def _dispatch(self, method: str) -> None:
        try:
            self._require_auth()
            parts = [unquote(part) for part in urlsplit(self.path).path.split("/") if part]
            rest = parts[2:] if parts[:2] == ["api", "v1"] else []
            route = _match_route(method, rest)
            if route is None:
                if _route_shape_matches(rest):
                    raise ControllerError(
                        405, "method_not_allowed", f"{method} is not allowed here"
                    )
                raise ControllerError(404, "not_found", f"no route for {method} {self.path}")
            name, captured = route
            payload = self._read_json() if method == "POST" else {}
            status, body = self._handle(name, captured, payload)
            self._send_json(status, body)
        except ControllerError as exc:
            with contextlib.suppress(Exception):
                self._send_json(exc.status, {"error": {"code": exc.code, "message": exc.message}})
        except Exception:  # pragma: no cover - defensive
            _log.exception("unhandled controller error")
            with contextlib.suppress(Exception):
                self._send_json(
                    500, {"error": {"code": "internal", "message": "internal controller error"}}
                )

    def _handle(
        self, name: str, captured: dict[str, str], payload: Mapping[str, Any]
    ) -> tuple[int, dict[str, Any] | None]:
        controller = self.server.controller
        if name == "register":
            return 200, controller.register_worker(payload)
        if name == "heartbeat":
            return 200, controller.heartbeat(captured["worker_id"], payload)
        if name == "stop":
            controller.stop_worker(captured["worker_id"], mode=_stop_mode(payload))
            return 204, None
        if name == "resume":
            controller.resume_worker(captured["worker_id"])
            return 204, None
        if name == "enqueue":
            return 200, controller.enqueue(payload)
        if name == "claim":
            job = controller.claim(_require_str(payload, "worker_id"))
            return (200, job) if job is not None else (204, None)
        if name == "complete":
            controller.complete(payload)
            return 204, None
        if name == "fail":
            controller.fail(payload)
            return 204, None
        if name == "events":
            params = parse_qs(urlsplit(self.path).query)
            after = _query_int(params, "after")
            limit = _query_int(params, "limit")
            return 200, controller.events(
                after=after, limit=limit if limit is not None else EVENTS_PAGE_LIMIT
            )
        if name == "workers":
            return 200, controller.workers()
        if name == "summary":
            params = parse_qs(urlsplit(self.path).query)
            run_id = params.get("run_id", [None])[0]
            return 200, controller.summary(run_id=run_id)
        raise ControllerError(500, "internal", f"unhandled route {name}")  # pragma: no cover

    def _require_auth(self) -> None:
        header = self.headers.get("Authorization", "")
        prefix = "Bearer "
        token = header[len(prefix) :] if header.startswith(prefix) else ""
        if not token or not hmac.compare_digest(token, self.server.controller.token):
            raise ControllerError(401, "unauthorized", "missing or invalid bearer token")

    def _read_json(self) -> Mapping[str, Any]:
        length_header = self.headers.get("Content-Length")
        if length_header is None:
            return {}
        try:
            length = int(length_header)
        except ValueError:
            raise ControllerError(400, "bad_request", "invalid Content-Length") from None
        if length < 0 or length > MAX_BODY_BYTES:
            raise ControllerError(400, "bad_request", "request body is too large")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            raise ControllerError(400, "bad_request", "body is not valid JSON") from None
        if not isinstance(data, dict):
            raise ControllerError(400, "bad_request", "body must be a JSON object")
        return data

    def _send_json(self, status: int, payload: dict[str, Any] | None) -> None:
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        if body:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if body:
            self.wfile.write(body)
        self.close_connection = True


def _match_route(method: str, rest: list[str]) -> tuple[str, dict[str, str]] | None:
    for route_method, segments, name in _ROUTES:
        if route_method != method or len(segments) != len(rest):
            continue
        captured: dict[str, str] = {}
        for segment, value in zip(segments, rest, strict=True):
            if segment is _WORKER_ID_SEGMENT:
                captured["worker_id"] = value
            elif segment != value:
                break
        else:
            return name, captured
    return None


def _route_shape_matches(rest: list[str]) -> bool:
    return any(
        len(segments) == len(rest)
        and all(
            segment is _WORKER_ID_SEGMENT or segment == value
            for segment, value in zip(segments, rest, strict=True)
        )
        for _, segments, _ in _ROUTES
    )


def _query_int(params: Mapping[str, list[str]], name: str) -> int | None:
    values = params.get(name)
    if not values:
        return None
    try:
        return int(values[0])
    except ValueError:
        raise ControllerError(400, "bad_request", f"{name!r} must be an integer") from None
