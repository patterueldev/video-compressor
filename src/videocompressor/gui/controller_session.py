"""Host-or-attach controller session for the GUI (issue #9).

plan.md: the GUI process is the controller. This module starts the in-process
``ControllerServer`` with the configured bind address and token, and falls back
to attaching to a controller v1 endpoint that already owns the address (for
example a ``vc controller`` run), so the monitor works in both topologies.
Pure Python — no PySide6 — so hosting and attach selection stay CI-testable.
"""

from __future__ import annotations

from pathlib import Path

from videocompressor.controller.protocol import ControllerError
from videocompressor.controller.server import ControllerServer, new_token, open_manifest
from videocompressor.controller.store import ControllerStore, default_db_path
from videocompressor.core.config import AppConfig, default_manifest_path, save_config
from videocompressor.gui.monitor_client import HttpControllerClient, MonitorUnavailable

__all__ = [
    "ControllerAttachError",
    "ControllerBindError",
    "ControllerSession",
    "connect_host",
    "start_controller_session",
]

DEFAULT_ATTACH_TIMEOUT_S = 2.0


class ControllerAttachError(RuntimeError):
    """Something is listening on the controller address but cannot be attached to."""


class ControllerBindError(RuntimeError):
    """The controller could not be started and no existing controller could be attached."""


class ControllerSession:
    """A live controller connection: hosted in this process, or attached over HTTP."""

    def __init__(
        self,
        *,
        client: HttpControllerClient,
        hosted: bool,
        description: str,
        server: ControllerServer | None = None,
    ) -> None:
        self.client = client
        self.hosted = hosted
        self.description = description
        self._server = server

    def close(self) -> None:
        """Stop the hosted controller (no-op when attached); safe to call twice."""
        if self._server is not None:
            self._server.stop()
            self._server = None


def start_controller_session(
    config: AppConfig,
    *,
    config_path: Path | None = None,
    store_path: Path | None = None,
    manifest_path: Path | None = None,
    timeout: float = DEFAULT_ATTACH_TIMEOUT_S,
) -> ControllerSession:
    """Host the controller, or attach to one already serving the configured address."""
    connect = connect_host(config.controller.host)
    base_url = f"http://{connect}:{config.controller.port}"

    if config.controller.token is not None:
        attached = _try_attach(base_url, config.controller.token, timeout)
        if attached is not None:
            return attached

    if config.controller.token is None:
        config.controller.token = new_token()
        save_config(config, config_path)

    store = ControllerStore.open(store_path if store_path is not None else default_db_path())
    manifest, durable = open_manifest(
        manifest_path if manifest_path is not None else default_manifest_path(),
        lease_ttl_seconds=config.controller.orphan_grace_seconds,
    )
    server = ControllerServer(
        store=store,
        manifest=manifest,
        token=config.controller.token,
        host=config.controller.host,
        port=config.controller.port,
        templates=config.templates,
        roots=lambda: config.roots,
        lease_ttl_seconds=config.controller.orphan_grace_seconds,
        capture_mode=config.capture_time.mode,
    )
    try:
        server.start_background()
    except OSError as error:
        store.close()
        if durable:
            manifest.close()
        client = HttpControllerClient(base_url, config.controller.token, timeout=timeout)
        try:
            client.probe()
        except (ControllerError, MonitorUnavailable):
            raise ControllerBindError(
                f"cannot listen on {config.controller.host}:{config.controller.port}:"
                f" {error}; another controller may already be running"
            ) from error
        return ControllerSession(
            client=client,
            hosted=False,
            description=f"attached to controller at {base_url}",
        )

    url = f"http://{connect}:{server.port}"
    description = f"hosting controller at {url}"
    if not durable:
        description += " (in-memory queue: clips will not survive a restart)"
    return ControllerSession(
        client=HttpControllerClient(url, config.controller.token, timeout=timeout),
        hosted=True,
        description=description,
        server=server,
    )


def _try_attach(base_url: str, token: str, timeout: float) -> ControllerSession | None:
    """Attach to a controller v1 endpoint; ``None`` when nothing is listening."""
    client = HttpControllerClient(base_url, token, timeout=timeout)
    try:
        client.probe()
    except MonitorUnavailable:
        return None
    except ControllerError as error:
        if error.status == 401:
            raise ControllerAttachError(
                f"a controller is already listening at {base_url} but the configured token"
                " was rejected; check [controller].token or stop that controller"
            ) from error
        raise ControllerAttachError(
            f"something is listening at {base_url} but it is not a controller v1 endpoint:"
            f" {error.message}"
        ) from error
    return ControllerSession(
        client=client,
        hosted=False,
        description=f"attached to controller at {base_url}",
    )


def connect_host(host: str) -> str:
    """Use the loopback address to reach a wildcard bind."""
    if host in {"0.0.0.0", "::"}:
        return "127.0.0.1"
    return host


#: Backward-compatible alias for the pre-#52 private name.
_connect_host = connect_host
