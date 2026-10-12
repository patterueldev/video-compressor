"""Render and push the per-node worker config (issue #53).

The pushed file is the ``worker.toml`` shape owned by ``worker/config.py`` and
documented in ``docs/worker-runbook.md``; a round-trip test loads the rendered
text through :func:`videocompressor.worker.config.load_worker_config`. The
token is written with mode 0600, and a differing pre-existing config is kept
as ``worker.toml.bak`` before it is replaced.
"""

from __future__ import annotations

import shlex
from pathlib import PurePosixPath
from urllib.parse import urlsplit

import tomli_w

from videocompressor.node.models import InstallError, RemoteValidationError, WorkerSpec
from videocompressor.node.ssh import SshRunner
from videocompressor.worker.config import WORKER_ID_PATTERN

__all__ = ["CONFIG_FILENAME", "push_worker_config", "render_worker_toml", "worker_config_path"]

CONFIG_FILENAME = "worker.toml"


def worker_config_path(install_dir: str) -> str:
    """The managed worker config path on the remote host."""
    return str(PurePosixPath(install_dir) / CONFIG_FILENAME)


def render_worker_toml(spec: WorkerSpec) -> str:
    """Render the worker config TOML for ``spec`` (validated first)."""
    _validate(spec)
    worker: dict[str, object] = {"id": spec.worker_id}
    if spec.name:
        worker["name"] = spec.name
    worker["controller_url"] = spec.controller_url
    worker["token"] = spec.token
    worker["scratch_dir"] = spec.scratch_dir
    worker["thread_cap"] = spec.thread_cap
    worker["poll_seconds"] = spec.poll_seconds
    document: dict[str, object] = {"worker": worker, "roots": dict(spec.roots)}
    return tomli_w.dumps(document)


def push_worker_config(runner: SshRunner, *, config_path: str, text: str) -> str:
    """Push ``text`` atomically; return a human detail for the report."""
    quoted = shlex.quote(config_path)
    if runner.run(f"test -f {quoted}").returncode == 0:
        existing = runner.run(f"cat {quoted}")
        if existing.returncode == 0 and existing.stdout == text:
            return "unchanged"
        backup = f"{config_path}.bak"
        result = runner.run(
            f"cp -p {quoted} {shlex.quote(backup)} && chmod 600 {shlex.quote(backup)}"
        )
        if result.returncode != 0:
            detail = (result.stderr or "").strip() or f"exit {result.returncode}"
            raise InstallError(f"could not back up {config_path}: {detail}")
        runner.write_text(config_path, text)
        return f"updated (previous config kept at {backup})"
    runner.write_text(config_path, text)
    return "created"


def _validate(spec: WorkerSpec) -> None:
    worker_id = spec.worker_id or ""
    if not WORKER_ID_PATTERN.fullmatch(worker_id):
        raise RemoteValidationError(
            f"worker id {worker_id!r} must match [A-Za-z0-9_.-]{{1,64}}"
        )
    split = urlsplit(spec.controller_url)
    if split.scheme not in ("http", "https") or not split.netloc:
        raise RemoteValidationError(
            f"controller URL {spec.controller_url!r} must be an http(s) URL"
        )
    if split.path not in ("", "/") or split.query or split.fragment:
        raise RemoteValidationError(
            f"controller URL {spec.controller_url!r} must not carry a path "
            "(the worker appends /api/v1)"
        )
    if not spec.token:
        raise RemoteValidationError("a worker token is required")
    if not spec.roots:
        raise RemoteValidationError("at least one --root mapping is required")
    for root_id, path in spec.roots.items():
        if not WORKER_ID_PATTERN.fullmatch(root_id):
            raise RemoteValidationError(
                f"root id {root_id!r} must match [A-Za-z0-9_.-]{{1,64}}"
            )
        if not path:
            raise RemoteValidationError(f"root {root_id!r} needs a remote path")
    if not spec.scratch_dir:
        raise RemoteValidationError("a remote scratch directory is required")
    if spec.thread_cap < 1:
        raise RemoteValidationError("thread cap must be a positive integer")
