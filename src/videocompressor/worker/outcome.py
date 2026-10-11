"""The per-job result type shared by the pipeline and the destination flow (issue #40).

``JobOutcome`` lives here rather than in :mod:`videocompressor.worker.pipeline` so
the destination module can return outcomes without importing the pipeline (and
without a circular import). The pipeline re-exports it, so existing imports keep
working.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from videocompressor.core.models import Fingerprint

__all__ = ["Disposition", "JobOutcome"]

Disposition = Literal["complete", "review", "failed", "requeue"]


@dataclass(frozen=True)
class JobOutcome:
    """What the agent should report for one settled job."""

    disposition: Disposition
    detail: str | None = None
    output: Fingerprint | None = None
    pilot: bool = False
    skipped: bool = False
    destination: Path | None = None
