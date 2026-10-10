"""Host-or-attach controller session (issue #9)."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from videocompressor.controller.memory_manifest import MemoryManifest
from videocompressor.controller.server import ControllerServer
from videocompressor.controller.store import ControllerStore
from videocompressor.core.config import AppConfig, load_config
from videocompressor.gui.controller_session import (
    ControllerAttachError,
    ControllerBindError,
    start_controller_session,
)
from videocompressor.gui.monitor_client import HttpControllerClient, MonitorUnavailable


def _config(
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    token: str | None = "test-token",
) -> AppConfig:
    config = AppConfig()
    config.controller.host = host
    config.controller.port = port
    config.controller.token = token
    return config


def _start(tmp_path: Path, config: AppConfig, **overrides: object):
    return start_controller_session(
        config,
        config_path=overrides.pop("config_path", tmp_path / "config.toml"),
        store_path=overrides.pop("store_path", tmp_path / "controller.sqlite"),
        manifest_path=overrides.pop("manifest_path", tmp_path / "manifest.sqlite"),
        **overrides,  # type: ignore[arg-type]
    )


def test_host_mode_serves_the_contract_and_frees_the_port_on_close(tmp_path):
    session = _start(tmp_path, _config())
    url = session.client.base_url
    try:
        assert session.hosted is True
        assert session.description.startswith("hosting controller at http://127.0.0.1:")
        assert session.client.workers() == {"workers": []}
    finally:
        session.close()
    session.close()  # idempotent
    with pytest.raises(MonitorUnavailable):
        HttpControllerClient(url, "test-token", timeout=0.5).probe()


def test_host_mode_generates_and_saves_a_missing_token(tmp_path):
    config_path = tmp_path / "config.toml"
    config = _config(token=None)
    session = _start(tmp_path, config, config_path=config_path)
    try:
        assert config.controller.token
        reloaded = load_config(config_path)
        assert reloaded.controller.token == config.controller.token
    finally:
        session.close()


def test_wildcard_bind_uses_loopback_for_the_client(tmp_path):
    session = _start(tmp_path, _config(host="0.0.0.0"))
    try:
        assert "http://127.0.0.1:" in session.description
        assert session.client.workers() == {"workers": []}
    finally:
        session.close()


def test_attach_mode_uses_an_existing_controller(tmp_path):
    store = ControllerStore.open(tmp_path / "existing.sqlite")
    server = ControllerServer(store=store, manifest=MemoryManifest(), token="shared", port=0)
    server.start_background()
    try:
        config = _config(port=server.port, token="shared")
        session = _start(tmp_path, config)
        try:
            assert session.hosted is False
            assert session.description == f"attached to controller at {server.base_url}"
            assert session.client.workers() == {"workers": []}
        finally:
            session.close()
        # the external controller keeps serving after the session closes
        assert HttpControllerClient(server.base_url, "shared", timeout=0.5).workers() == {
            "workers": []
        }
    finally:
        server.stop()


def test_token_mismatch_is_reported(tmp_path):
    store = ControllerStore.open(tmp_path / "existing.sqlite")
    server = ControllerServer(store=store, manifest=MemoryManifest(), token="right", port=0)
    server.start_background()
    try:
        with pytest.raises(ControllerAttachError) as excinfo:
            _start(tmp_path, _config(port=server.port, token="wrong"))
        assert "token" in str(excinfo.value)
        assert "stop that controller" in str(excinfo.value)
    finally:
        server.stop()


def test_busy_port_that_is_not_a_controller_fails_clearly(tmp_path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        with pytest.raises(ControllerBindError) as excinfo:
            _start(tmp_path, _config(port=port), timeout=0.2)
        assert "another controller may already be running" in str(excinfo.value)
