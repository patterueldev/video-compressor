"""Crash recovery for the replacement protocol: reconcile a source path with any
staged-temp or backup artifacts left behind by an interrupted swap.

``reconcile_source`` is idempotent: once it has repaired a crash point, running it
again makes no further changes. It never guesses at an ambiguous state (several
backups), and it never deletes a file that could be the only copy: the original is
restored whenever the replacement cannot be confirmed readable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from videocompressor.core.replace import (
    BACKUP_MARKER,
    ConfirmFn,
    SwapArtifacts,
    ffprobe_readable,
    find_artifacts,
)

#: Name component used to park backups whose job cannot be attributed (anomaly).
REVIEW_MARKER = ".vc-review."


class RecoveryOutcome(StrEnum):
    """What reconciliation found and did for one source path."""

    NOTHING_TO_DO = "nothing_to_do"
    SOURCE_MISSING = "source_missing"
    TEMP_REMOVED = "temp_removed"
    ORIGINAL_RESTORED = "original_restored"
    REPLACEMENT_CONFIRMED = "replacement_confirmed"
    NEEDS_REVIEW = "needs_review"


@dataclass(frozen=True)
class RecoveryReport:
    """Result of reconciling one source path; suitable for a run log or review queue."""

    source: Path
    outcome: RecoveryOutcome
    temps_removed: tuple[Path, ...] = ()
    backups_removed: tuple[Path, ...] = ()
    restored_from: Path | None = None
    review_paths: tuple[Path, ...] = ()
    detail: str | None = None


def reconcile_source(
    source: Path, *, confirm: ConfirmFn | None = None
) -> RecoveryReport:
    """Repair the crash state around ``source`` and report what happened.

    Crash-point matrix, given the protocol's step order (stage → backup rename →
    temp rename → confirm → backup delete):

    - ``source`` present, temp(s) only: the swap never started; remove the temps.
    - ``source`` missing, backup present: crash between the two renames; rename the
      backup back to the original path.
    - ``source`` missing, backup + temp(s): crash between the two renames; restore
      the backup and remove the temps (the temp is an unverifiable copy).
    - ``source`` + backup, replacement readable: crash between the temp rename and
      the backup delete; the swap succeeded, delete the backup.
    - ``source`` + backup, replacement unreadable: restore the backup over the
      broken replacement.
    - several backups: never guess; restore the newest if the source is missing and
      park the rest as ``.vc-review.*`` for the operator.

    ``confirm`` defaults to the minimal ffprobe readability check from
    :mod:`videocompressor.core.replace`; passing a full validator is the caller's
    choice.
    """
    confirmer: ConfirmFn = confirm if confirm is not None else ffprobe_readable
    artifacts: SwapArtifacts = find_artifacts(source)
    temps = artifacts.temps
    backups = artifacts.backups

    if len(backups) > 1:
        return _reconcile_multiple_backups(source, temps, backups)

    if backups:
        return _reconcile_single_backup(source, temps, backups[0], confirmer)

    if temps:
        if source.exists():
            removed = _remove_temps(temps)
            return RecoveryReport(
                source=source,
                outcome=RecoveryOutcome.TEMP_REMOVED,
                temps_removed=removed,
                detail="swap had not started; staged temp(s) removed",
            )
        return RecoveryReport(
            source=source,
            outcome=RecoveryOutcome.SOURCE_MISSING,
            review_paths=temps,
            detail="source missing with no backup; staged temp(s) left for review",
        )

    if source.exists():
        return RecoveryReport(source=source, outcome=RecoveryOutcome.NOTHING_TO_DO)
    return RecoveryReport(
        source=source,
        outcome=RecoveryOutcome.SOURCE_MISSING,
        detail="source missing and no swap artifacts found",
    )


def _reconcile_single_backup(
    source: Path, temps: tuple[Path, ...], backup: Path, confirm: ConfirmFn
) -> RecoveryReport:
    if not source.exists():
        restore_error = _try_restore(source, backup)
        removed = _remove_temps(temps)
        if restore_error is not None:
            return RecoveryReport(
                source=source,
                outcome=RecoveryOutcome.NEEDS_REVIEW,
                temps_removed=removed,
                review_paths=(backup,),
                detail=f"crash between the renames; restoring the backup failed: {restore_error}",
            )
        return RecoveryReport(
            source=source,
            outcome=RecoveryOutcome.ORIGINAL_RESTORED,
            temps_removed=removed,
            restored_from=backup,
            detail="crash between the renames; original restored from backup",
        )

    readable, probe_detail = _confirm_quietly(confirm, source)
    if readable:
        removed = _remove_temps(temps)
        cleanup_error = _try_unlink_error(backup)
        if cleanup_error is not None:
            return RecoveryReport(
                source=source,
                outcome=RecoveryOutcome.NEEDS_REVIEW,
                temps_removed=removed,
                review_paths=(backup,),
                detail=f"replacement confirmed but backup cleanup failed: {cleanup_error}",
            )
        return RecoveryReport(
            source=source,
            outcome=RecoveryOutcome.REPLACEMENT_CONFIRMED,
            temps_removed=removed,
            backups_removed=(backup,),
            detail="replacement readable; backup removed",
        )

    restore_error = _try_restore(source, backup)
    removed = _remove_temps(temps)
    if restore_error is not None:
        return RecoveryReport(
            source=source,
            outcome=RecoveryOutcome.NEEDS_REVIEW,
            temps_removed=removed,
            review_paths=(backup,),
            detail=(
                f"replacement unreadable ({probe_detail}) and restoring the backup "
                f"failed: {restore_error}"
            ),
        )
    return RecoveryReport(
        source=source,
        outcome=RecoveryOutcome.ORIGINAL_RESTORED,
        temps_removed=removed,
        restored_from=backup,
        detail=f"replacement unreadable ({probe_detail}); original restored from backup",
    )


def _reconcile_multiple_backups(
    source: Path, temps: tuple[Path, ...], backups: tuple[Path, ...]
) -> RecoveryReport:
    ordered = sorted(backups, key=_mtime_ns)
    newest, extras = ordered[-1], ordered[:-1]

    if source.exists():
        removed = _remove_temps(temps)
        return RecoveryReport(
            source=source,
            outcome=RecoveryOutcome.NEEDS_REVIEW,
            temps_removed=removed,
            review_paths=backups,
            detail="several backups present; none touched until reviewed",
        )

    restore_error = _try_restore(source, newest)
    removed = _remove_temps(temps)
    if restore_error is not None:
        return RecoveryReport(
            source=source,
            outcome=RecoveryOutcome.NEEDS_REVIEW,
            temps_removed=removed,
            review_paths=backups,
            detail=(
                f"several backups present and restoring the newest failed: {restore_error}"
            ),
        )

    parked: list[Path] = []
    stuck: list[Path] = []
    for extra in extras:
        parked_path = _park_for_review(source, extra)
        if parked_path is not None:
            parked.append(parked_path)
        else:
            stuck.append(extra)
    detail = "several backups present; newest restored and the rest parked as review files"
    if stuck:
        detail += f"; {len(stuck)} backup(s) could not be parked"
    return RecoveryReport(
        source=source,
        outcome=RecoveryOutcome.NEEDS_REVIEW,
        temps_removed=removed,
        restored_from=newest,
        review_paths=tuple(parked + stuck),
        detail=detail,
    )


def _confirm_quietly(confirm: ConfirmFn, path: Path) -> tuple[bool, str | None]:
    try:
        return bool(confirm(path)), None
    except Exception as exc:  # a failing probe must never abort recovery
        return False, f"probe error: {exc}"


def _try_restore(source: Path, backup: Path) -> str | None:
    try:
        os.replace(backup, source)
    except OSError as exc:
        return str(exc)
    return None


def _try_unlink_error(path: Path) -> str | None:
    try:
        path.unlink()
    except OSError as exc:
        return str(exc)
    return None


def _remove_temps(temps: tuple[Path, ...]) -> tuple[Path, ...]:
    removed: list[Path] = []
    for temp in temps:
        if _try_unlink_error(temp) is None:
            removed.append(temp)
    return tuple(removed)


def _park_for_review(source: Path, backup: Path) -> Path | None:
    suffix = backup.name[len(source.name + BACKUP_MARKER) :]
    parked = backup.with_name(source.name + REVIEW_MARKER + suffix)
    counter = 1
    while parked.exists():
        parked = backup.with_name(f"{source.name}{REVIEW_MARKER}{suffix}.{counter}")
        counter += 1
    try:
        backup.rename(parked)
    except OSError:
        return None
    return parked


def _mtime_ns(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0
