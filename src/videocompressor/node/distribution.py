"""Local wheelhouse preparation for wheel-over-SSH distribution (issue #53).

The chosen distribution ships a wheelhouse (the project wheel plus its
pure-Python runtime dependencies) over SSH, so the remote host needs no
network and no build toolchain. Pass a prebuilt ``--wheelhouse`` directory, or
let this module build one with ``python -m pip wheel`` from a project checkout.
Tests inject the command runner; the build path never runs in CI.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from videocompressor.node.models import WheelhouseError

__all__ = ["Wheelhouse", "find_project_wheel", "prepare_wheelhouse"]

#: Wheel filenames start with the normalized project name.
WHEEL_PREFIX = "video_compressor-"


class CommandRunner(Protocol):
    """The local subprocess seam (``subprocess.run`` by default)."""

    def __call__(
        self, argv: list[str], *, capture_output: bool, text: bool
    ) -> subprocess.CompletedProcess[str]: ...


@dataclass(frozen=True)
class Wheelhouse:
    """A prepared wheel directory; ``temporary`` marks a local build."""

    path: Path
    temporary: bool = False


def find_project_wheel(wheelhouse: Path) -> Path:
    """Return the newest project wheel in ``wheelhouse`` or raise."""
    wheels = sorted(wheelhouse.glob(f"{WHEEL_PREFIX}*.whl"))
    if not wheels:
        raise WheelhouseError(
            f"no {WHEEL_PREFIX}*.whl wheel in {wheelhouse}; "
            "pass a wheelhouse that contains the project wheel"
        )
    return wheels[-1]


def prepare_wheelhouse(
    *,
    wheelhouse: Path | None,
    source_dir: Path,
    runner: CommandRunner | None = None,
) -> Wheelhouse:
    """Validate a provided wheelhouse, or build one from a project checkout."""
    if wheelhouse is not None:
        resolved = wheelhouse.expanduser().resolve()
        if not resolved.is_dir():
            raise WheelhouseError(f"wheelhouse {resolved} is not a directory")
        find_project_wheel(resolved)
        return Wheelhouse(resolved, temporary=False)

    source = source_dir.expanduser().resolve()
    if not (source / "pyproject.toml").is_file():
        raise WheelhouseError(
            f"{source} has no pyproject.toml; run from the project checkout "
            "or pass --wheelhouse DIR with prebuilt wheels"
        )
    target = Path(tempfile.mkdtemp(prefix="vc-wheelhouse-"))
    argv = [
        sys.executable,
        "-m",
        "pip",
        "wheel",
        "--wheel-dir",
        str(target),
        str(source),
    ]
    invoke = runner if runner is not None else subprocess.run
    try:
        proc = invoke(argv, capture_output=True, text=True)
    except OSError as exc:
        raise WheelhouseError(f"could not run pip to build the wheelhouse: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[-400:] or f"exit {proc.returncode}"
        raise WheelhouseError(f"building the wheelhouse failed: {detail}")
    find_project_wheel(target)
    return Wheelhouse(target, temporary=True)
