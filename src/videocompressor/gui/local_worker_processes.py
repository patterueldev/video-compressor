"""Stale app-managed local-worker reaping (issue #70).

Pure Python — no PySide6. An app killed with SIGTERM (or that crashed) can leave
its managed child running; the orphan keeps the code it loaded at its start,
keeps claiming under the same worker id, and collides with the new child's
startup scratch sweep (``worker/scratch.py`` wipes ``jobs/`` at start). The
supervisor sweeps the process table for processes still running the app-generated
``local-worker.toml`` before it spawns a new child.

The generated config path on ``--config`` is the only match target: a standalone
worker's config lives elsewhere and is never touched. The process table is a
seam, so every test stays off real processes.
"""

from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
import time
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

__all__ = [
    "PS_ARGV",
    "ProcessInfo",
    "ProcessTable",
    "STALE_POLL_SECONDS",
    "SystemProcessTable",
    "parse_ps",
    "stale_worker_pids",
    "terminate_stale_workers",
]

_log = logging.getLogger("videocompressor.gui")

#: ``-ww`` keeps long command lines out of the terminal-width truncation.
#: BSD and procps ``ps`` both accept this form.
PS_ARGV = ("ps", "-ww", "-axo", "pid=,command=")

#: Poll cadence while waiting for a signalled process to disappear.
STALE_POLL_SECONDS = 0.1

_PS_LINE = re.compile(r"^\s*(\d+)\s+(.*)$")


@dataclass(frozen=True)
class ProcessInfo:
    """One process-table row: the pid and its full command line."""

    pid: int
    command: str


class ProcessTable(Protocol):
    """The slice of the process table the stale sweep uses."""

    def list_processes(self) -> Sequence[ProcessInfo]: ...

    def signal(self, pid: int, signum: int) -> None: ...


class SystemProcessTable:
    """Default table: ``ps`` for listing, ``os.kill`` for signals."""

    def list_processes(self) -> tuple[ProcessInfo, ...]:
        """Every readable process, or an empty table when ``ps`` is unavailable."""
        try:
            completed = subprocess.run(PS_ARGV, capture_output=True, text=True, check=False)
        except OSError as error:
            _log.warning("cannot read the process table: %s", error)
            return ()
        if completed.returncode != 0:
            _log.warning("ps exited with %s; sweeping no processes", completed.returncode)
            return ()
        return parse_ps(completed.stdout)

    def signal(self, pid: int, signum: int) -> None:
        os.kill(pid, signum)


def parse_ps(output: str) -> tuple[ProcessInfo, ...]:
    """Parse ``ps -ww -axo pid=,command=`` output; unparseable lines are skipped."""
    processes: list[ProcessInfo] = []
    for line in output.splitlines():
        match = _PS_LINE.match(line)
        if match is not None:
            processes.append(ProcessInfo(int(match.group(1)), match.group(2)))
    return tuple(processes)


def stale_worker_pids(
    processes: Sequence[ProcessInfo],
    config_path: Path,
    *,
    exclude_pids: Collection[int] = (),
) -> list[int]:
    """PIDs running the app-generated worker config, excluding ``exclude_pids``.

    Matching is exact: the command line must pass the resolved ``config_path``
    through ``--config`` (either form). A standalone worker uses another config
    path, and a command that merely mentions the file (an editor, ``cat``) does
    not match.
    """
    target = str(config_path.resolve())
    excluded = set(exclude_pids)
    return [
        process.pid
        for process in processes
        if process.pid not in excluded and _runs_config(process.command, target)
    ]


def terminate_stale_workers(
    config_path: Path,
    *,
    table: ProcessTable,
    exclude_pids: Collection[int] = (),
    terminate_timeout: float,
    kill_timeout: float,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> list[int]:
    """SIGTERM/SIGKILL every stale worker for ``config_path``; return confirmed-gone pids.

    Best effort: a process that cannot be signalled (already gone, or not ours)
    is logged and skipped, and a SIGKILL survivor is not returned, so the caller
    never records a stop for a worker that may still be running.
    """
    targets = stale_worker_pids(table.list_processes(), config_path, exclude_pids=exclude_pids)
    if not targets:
        return []
    for pid in targets:
        _signal(table, pid, signal.SIGTERM)
    remaining = _wait_until_gone(
        table, targets, timeout=terminate_timeout, sleep=sleep, clock=clock
    )
    if remaining:
        for pid in remaining:
            _signal(table, pid, signal.SIGKILL)
        remaining = _wait_until_gone(
            table, remaining, timeout=kill_timeout, sleep=sleep, clock=clock
        )
        for pid in remaining:
            _log.warning("stale local worker process %s did not stop", pid)
    survivors = set(remaining)
    return [pid for pid in targets if pid not in survivors]


def _runs_config(command: str, target: str) -> bool:
    return f"--config {target}" in command or f"--config={target}" in command


def _signal(table: ProcessTable, pid: int, signum: int) -> None:
    try:
        table.signal(pid, signum)
    except ProcessLookupError:
        pass  # already gone: the process list check decides confirmation
    except OSError as error:
        _log.warning("cannot signal stale local worker process %s: %s", pid, error)


def _wait_until_gone(
    table: ProcessTable,
    pids: Sequence[int],
    *,
    timeout: float,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> list[int]:
    """The subset of ``pids`` still listed after at most ``timeout`` seconds."""
    deadline = clock() + timeout
    target = set(pids)
    while True:
        listed = {process.pid for process in table.list_processes()}
        remaining = sorted(target & listed)
        if not remaining or clock() >= deadline:
            return remaining
        sleep(STALE_POLL_SECONDS)
