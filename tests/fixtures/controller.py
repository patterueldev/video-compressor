"""Controller-domain fixtures (issues #5, #7, and later)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta

import pytest

from videocompressor.controller.memory_manifest import MemoryManifest
from videocompressor.controller.server import ControllerServer
from videocompressor.controller.store import ControllerStore
from videocompressor.core.models import ClipRecord, Fingerprint, JobState

TOKEN = "test-token"
_USE_DEFAULT_TOKEN = object()


class FakeClock:
    """Manually advanced clock for lease and timestamp tests."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 10, 10, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


class ApiClient:
    """Minimal bearer-token HTTP client for the controller API."""

    def __init__(self, server: ControllerServer, token: str | None = TOKEN) -> None:
        self.base_url = server.base_url + "/api/v1"
        self._token = token

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: object | None = None,
        body: bytes | None = None,
        token: object = _USE_DEFAULT_TOKEN,
    ) -> tuple[int, dict | None]:
        if body is None and payload is not None:
            body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(self.base_url + path, data=body, method=method)
        request.add_header("Content-Type", "application/json")
        effective_token = self._token if token is _USE_DEFAULT_TOKEN else token
        if effective_token is not None:
            request.add_header("Authorization", f"Bearer {effective_token}")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                raw = response.read()
                return response.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            return exc.code, (json.loads(raw) if raw else None)

    def post(self, path: str, payload: object | None = None, **kwargs) -> tuple[int, dict | None]:
        return self.request("POST", path, payload=payload, **kwargs)

    def get(self, path: str, **kwargs) -> tuple[int, dict | None]:
        return self.request("GET", path, **kwargs)

    def register(self, worker_id: str, **extra: object) -> dict:
        payload = {
            "worker_id": worker_id,
            "name": worker_id,
            "roots": {"archive": "/srv/archive"},
            "scratch_dir": "/srv/scratch",
            "thread_cap": 2,
            "version": "0.1.0",
        }
        payload.update(extra)
        status, body = self.post("/workers/register", payload)
        assert status == 200, body
        assert body is not None
        return body


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store(tmp_path, clock):
    controller_store = ControllerStore.open(tmp_path / "controller.sqlite", clock=clock)
    yield controller_store
    controller_store.close()


@pytest.fixture
def manifest(clock) -> MemoryManifest:
    return MemoryManifest(clock=clock)


@pytest.fixture
def server(store, manifest, clock):
    controller = ControllerServer(
        store=store,
        manifest=manifest,
        token=TOKEN,
        port=0,
        clock=clock,
        sweep_interval_seconds=3600,
    )
    controller.start_background()
    yield controller
    controller.stop()


@pytest.fixture
def api(server) -> ApiClient:
    return ApiClient(server)


@pytest.fixture
def make_api():
    """Build an ApiClient for a server constructed inside a test."""

    def _make(controller: ControllerServer, token: str | None = TOKEN) -> ApiClient:
        return ApiClient(controller, token=token)

    return _make


@pytest.fixture
def seed_clip(manifest, clock):
    """Seed a clip record directly into the manifest (the queue comes from #3/#8)."""

    def _seed(
        relative_path: str = "2024-06-Ride/GS010001.MP4",
        *,
        root_id: str = "archive",
        state: JobState = JobState.QUEUED,
        **fields: object,
    ) -> ClipRecord:
        record = ClipRecord(
            root_id=root_id,
            relative_path=relative_path,
            source=Fingerprint(size_bytes=4096, mtime_ns=1, digest="source-digest"),
            state=state,
            created_at=clock(),
            updated_at=clock(),
            **fields,
        )
        manifest.upsert_clip(record)
        return record

    return _seed
