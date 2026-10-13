"""Machine-aware duplicate decision for the app-managed local worker (issue #80).

Pure Python and PySide6-free. Since issue #80 a worker registration carries a
machine identity (the agent's hostname); a registration with a different worker
id covers this machine only when that identity matches, so a remote node whose
share maps to the same local paths no longer blocks the default local worker.
Registrations from agents older than #80 carry no identity and keep the pre-#80
root-mapping heuristic; either way a registration whose last heartbeat predates
a confirmed stop is a dead row, not a live node (#52, #70).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from videocompressor.gui.local_worker_marker import stopped_before
from videocompressor.gui.monitor_service import WorkerStatus

__all__ = ["find_duplicate"]


def find_duplicate(
    workers: tuple[WorkerStatus, ...],
    *,
    worker_id: str,
    roots: Mapping[str, str],
    local_machine: str | None,
    stopped_at: datetime | None,
) -> WorkerStatus | None:
    """The online registration that already covers this machine, or ``None``.

    Same worker id always covers (the #52 rule). A different id covers only
    when both this machine and the registration carry an identity and they are
    equal; a registration without one keeps the root-mapping heuristic. A row
    whose heartbeat predates a confirmed stop is skipped as dead.
    """
    expected = dict(roots)
    for worker in workers:
        if not worker.online:
            continue
        if worker.worker_id == worker_id:
            covered = True
        elif local_machine and worker.machine_id:
            covered = worker.machine_id == local_machine
        else:
            covered = worker.roots == expected
        if not covered:
            continue
        if stopped_at is not None and stopped_before(worker.last_seen, stopped_at):
            continue  # a dead registration: heartbeat predates our confirmed stop
        return worker
    return None
