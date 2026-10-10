"""Transfer helpers for the worker pipeline (issue #6).

Two delivery paths share the same verified-copy idea:

- :func:`replace_at_source` wraps ``core.replace.replace_with_verified_copy`` with
  the full output validator as the readability confirmation and reports the
  swap boundary to the agent for stage tracking;
- :func:`deliver_to_pilot` copies the validated output into the pilot root
  (mirroring the archive-relative path) and verifies the copy by read-back; the
  source is never touched.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from videocompressor.core.models import Fingerprint
from videocompressor.core.replace import (
    STEP_BACKUP_RENAMED,
    ReplaceResult,
    sha256_file,
)
from videocompressor.core.validate import MediaFacts
from videocompressor.worker.scratch import StopEvent, copy_with_stop

__all__ = ["PilotDeliveryError", "deliver_to_pilot", "replace_at_source"]

_log = logging.getLogger("videocompressor.worker")

FingerprintFn = Callable[[Path], Fingerprint]


class PilotDeliveryError(RuntimeError):
    """The pilot output copy did not verify against the local output."""


def replace_at_source(
    output: Path,
    source: Path,
    *,
    settings: object,
    source_facts: MediaFacts,
    expected_source: Fingerprint,
    job_id: str,
    fingerprint: FingerprintFn,
    replace: Callable[..., ReplaceResult],
    validate: Callable[..., object],
    on_phase: Callable[[str], None] | None = None,
) -> ReplaceResult:
    """Swap a validated local output into the source path behind a backup.

    The readability confirmation runs the full validator (codec, height,
    duration, audio, sampled decode) against the transferred file, as plan.md
    requires for local *and* transferred outputs.
    """

    def confirm(path: Path) -> bool:
        return bool(validate(path, settings, source=source_facts).ok)

    def on_step(step: str) -> None:
        if step == STEP_BACKUP_RENAMED and on_phase is not None:
            on_phase("replacing")

    return replace(
        output,
        source,
        expected_source=expected_source,
        job_id=job_id,
        fingerprint=fingerprint,
        confirm=confirm,
        on_step=on_step,
    )


def deliver_to_pilot(
    output: Path,
    pilot_root: Path,
    relative_path: str,
    stop_event: StopEvent,
    *,
    fingerprint: FingerprintFn,
) -> tuple[Path, Fingerprint]:
    """Copy the validated output into the pilot root, verify it, and fingerprint it.

    The destination mirrors the archive-relative POSIX path. The copy is
    verified by a full SHA-256 read-back of both files before the job is
    reported; the source clip is never touched.
    """
    destination = pilot_root.joinpath(*PurePosixPath(relative_path).parts)
    copy_with_stop(output, destination, stop_event)
    expected = sha256_file(output)
    actual = sha256_file(destination)
    if expected != actual:
        raise PilotDeliveryError(
            f"pilot copy did not verify: {destination} "
            f"(expected {expected[0]} bytes/{expected[1]}, found {actual[0]} bytes/{actual[1]})"
        )
    _log.info("pilot output delivered to %s", destination)
    return destination, fingerprint(destination)
