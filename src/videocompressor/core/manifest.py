"""Central manifest: clip records, job states, and leases.

Frozen in issue #1; the SQLite implementation lands in issue #3.
"""

from __future__ import annotations

from pathlib import Path
from typing import Self

from videocompressor.core.models import ClipRecord, JobState


class Manifest:
    """Durable clip/job store shared by the controller, workers, and the GUI."""

    @classmethod
    def open(cls, db_path: Path) -> Self:
        """Open (or create) the manifest database at ``db_path``."""
        raise NotImplementedError("Manifest is implemented in issue #3")

    def upsert_clip(self, record: ClipRecord) -> None:
        """Insert or update the record for one clip, keyed by root + relative path."""
        raise NotImplementedError("Manifest is implemented in issue #3")

    def list_clips(self, *, state: JobState | None = None) -> list[ClipRecord]:
        """List clip records, optionally filtered by state."""
        raise NotImplementedError("Manifest is implemented in issue #3")

    def claim_next(self, worker_id: str) -> ClipRecord | None:
        """Transactionally claim the next queued clip for ``worker_id``.

        Returns ``None`` when nothing is claimable. A clip must never be claimable
        by two workers at once (issue #7 race requirement).
        """
        raise NotImplementedError("Manifest is implemented in issue #3")

    def heartbeat(self, root_id: str, relative_path: str, worker_id: str) -> None:
        """Renew the lease on a claimed clip (90 s TTL, 15 s heartbeat; issue #7)."""
        raise NotImplementedError("Manifest is implemented in issue #3")

    def update_state(
        self, root_id: str, relative_path: str, state: JobState, **changes: object
    ) -> None:
        """Move a clip to ``state``; ``changes`` updates other ``ClipRecord`` fields."""
        raise NotImplementedError("Manifest is implemented in issue #3")

    def reconcile(self) -> list[ClipRecord]:
        """Recover expired leases; returns records needing operator review (issues #5/#7)."""
        raise NotImplementedError("Manifest is implemented in issue #3")
