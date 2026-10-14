"""Filesystem creation-date (birth-time) stamping for converted outputs (issue #86).

The encode carries the clip's capture time over into the container, but the
filesystem creation date of a fresh output is the moment it was written, so a
mixed media folder sorted by "Date Created" shows conversion dates. This module
is the single owner of the birth-time logic:

- read a file's creation time (macOS ``st_birthtime``; ``None`` elsewhere);
- stamp a creation time natively through ``setattrlist``/``ATTR_CMN_CRTIME``
  (no ``SetFile``/Xcode dependency), touching nothing else — the modification
  date stays the conversion time;
- sweep a folder recursively: resolve each video's capture time from its own
  embedded ``creation_time`` tag under the app's capture mode and a template
  timezone, then stamp it;
- a ``since`` filter skips files whose creation time is already older than the
  cutoff *without probing them* (the cheap post-batch path);
- idempotence: a file already showing the target value is ``already_correct``,
  so re-running is safe; ``dry_run`` classifies without mutating anything.

Linux cannot set birth times, so the sweep is an app-side (macOS) step: no
worker, controller, or protocol changes. Every per-file failure is recorded in
the report and never aborts the sweep.

The ``probe``/``stamp``/``read_birth`` seams are injectable so the fast tests
need neither ffmpeg nor macOS.
"""

from __future__ import annotations

import ctypes
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from videocompressor.core.capture_time import resolve_capture_time
from videocompressor.core.config import DEFAULT_TEMPLATE_NAME, AppConfig
from videocompressor.core.models import CaptureTime, CaptureTimeMode, MediaInfo, TemplateSettings
from videocompressor.core.probe import probe_media
from videocompressor.core.records import RECORDS_DIR

__all__ = [
    "VIDEO_SUFFIXES",
    "StampReport",
    "StampResult",
    "StampStatus",
    "read_creation_time",
    "stamp_creation_time",
    "summarize",
    "supported",
    "sweep_folder",
    "timezone_for_config",
]

#: Video files a sweep considers; its own canonical copy (never the GUI's).
VIDEO_SUFFIXES = frozenset({".mp4", ".mov"})

#: ``setattrlist`` attribute-group bitmap size (``ATTR_BIT_MAP_COUNT``).
_ATTR_BIT_MAP_COUNT = 5
#: ``ATTR_CMN_CRTIME`` from ``<sys/attr.h>``: the creation (birth) time.
_ATTR_CMN_CRTIME = 0x00000200


class StampStatus(StrEnum):
    """Outcome of one sweep candidate."""

    STAMPED = "stamped"
    WOULD_STAMP = "would_stamp"  # dry run: a candidate, nothing was written
    ALREADY_CORRECT = "already_correct"
    SKIPPED_NO_CAPTURE = "skipped_no_capture"
    SKIPPED_OLDER = "skipped_older"
    FAILED = "failed"


@dataclass(frozen=True)
class StampResult:
    """One video's sweep outcome; ``when`` is the resolved capture time."""

    path: Path
    status: StampStatus
    when: datetime | None = None
    raw: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class StampReport:
    """Counts and per-file outcomes of one sweep."""

    folder: Path
    mode: CaptureTimeMode
    timezone: str
    since: datetime | None
    dry_run: bool
    results: tuple[StampResult, ...]
    ignored: int = 0

    def count(self, status: StampStatus) -> int:
        """How many candidates ended with ``status``."""
        return sum(1 for result in self.results if result.status is status)

    @property
    def scanned(self) -> int:
        """Video files the sweep inspected (ignored files excluded)."""
        return len(self.results)

    @property
    def stamped(self) -> int:
        """Files whose creation date was actually set."""
        return self.count(StampStatus.STAMPED)

    @property
    def would_stamp(self) -> int:
        """Dry-run candidates that would have been stamped."""
        return self.count(StampStatus.WOULD_STAMP)

    @property
    def already_correct(self) -> int:
        """Files already showing the resolved capture time."""
        return self.count(StampStatus.ALREADY_CORRECT)

    @property
    def skipped_no_capture(self) -> int:
        """Files without a trustworthy embedded capture time."""
        return self.count(StampStatus.SKIPPED_NO_CAPTURE)

    @property
    def skipped_older(self) -> int:
        """Files skipped by the ``since`` filter before any probing."""
        return self.count(StampStatus.SKIPPED_OLDER)

    @property
    def failed(self) -> int:
        """Files whose probe or stamp failed; the sweep continued."""
        return self.count(StampStatus.FAILED)


def supported() -> bool:
    """True only on macOS, where a filesystem birth time can be set."""
    return sys.platform == "darwin"


def read_creation_time(path: Path) -> datetime | None:
    """The file's birth time as naive local time, or ``None`` when unavailable."""
    if not supported():
        return None
    try:
        info = path.stat()
    except OSError:
        return None
    birth = getattr(info, "st_birthtime", None)
    if birth is None:
        return None
    try:
        return datetime.fromtimestamp(birth)
    except (OSError, OverflowError, ValueError):
        return None


def stamp_creation_time(path: Path, when: datetime) -> bool:
    """Set ``path``'s birth time to ``when``; never raises, returns success.

    ``when`` is an instant. A naive value is read as local time; an aware value
    is converted to the same instant. Only the creation time is written, so the
    modification date is untouched.
    """
    if not supported():
        return False
    try:
        library = _setattrlist_library()
        if library is None:
            return False
        seconds = when.timestamp()
        tv_sec = int(seconds)
        tv_nsec = int(round((seconds - tv_sec) * 1_000_000_000))
        if tv_nsec >= 1_000_000_000:
            tv_sec += 1
            tv_nsec -= 1_000_000_000
        attributes = _AttrList(_ATTR_BIT_MAP_COUNT, 0, _ATTR_CMN_CRTIME, 0, 0, 0, 0)
        timespec = _TimeSpec(tv_sec, tv_nsec)
        result = library.setattrlist(
            os.fsencode(str(path)),
            ctypes.byref(attributes),
            ctypes.byref(timespec),
            ctypes.sizeof(_TimeSpec),
            0,
        )
    except Exception:  # a stamp failure must never escape
        return False
    return result == 0


def sweep_folder(
    folder: Path,
    *,
    mode: CaptureTimeMode,
    timezone: str,
    since: datetime | None = None,
    dry_run: bool = False,
    probe: Callable[[Path], MediaInfo] = probe_media,
    stamp: Callable[[Path, datetime], bool] = stamp_creation_time,
    read_birth: Callable[[Path], datetime | None] = read_creation_time,
) -> StampReport:
    """Stamp every video under ``folder`` with its resolved capture time.

    The walk is sorted and deterministic. ``.vc-records`` files and non-video
    suffixes are counted as ignored. With ``since``, a file whose known birth
    time is older is ``skipped_older`` without probing; with ``dry_run``,
    stampable candidates are ``would_stamp`` and nothing is mutated. A failed
    probe or stamp is recorded and the sweep continues.
    """
    results: list[StampResult] = []
    ignored = 0
    cutoff = _local_wall_clock(since) if since is not None else None
    for path in _iter_files(folder):
        if RECORDS_DIR in path.relative_to(folder).parts or (
            path.suffix.lower() not in VIDEO_SUFFIXES
        ):
            ignored += 1
            continue
        birth = read_birth(path)
        if cutoff is not None and birth is not None and birth < cutoff:
            results.append(StampResult(path, StampStatus.SKIPPED_OLDER))
            continue
        try:
            media = probe(path)
            capture = resolve_capture_time(media, mode=mode, timezone=timezone)
        except Exception as error:  # a bad file never aborts the sweep
            results.append(StampResult(path, StampStatus.FAILED, error=str(error)))
            continue
        results.append(_stamp_candidate(path, capture, birth, dry_run=dry_run, stamp=stamp))
    return StampReport(
        folder=folder,
        mode=mode,
        timezone=timezone,
        since=since,
        dry_run=dry_run,
        results=tuple(results),
        ignored=ignored,
    )


def summarize(report: StampReport) -> str:
    """One status line for the report: ``"129 stamped, 0 failed"``."""
    if report.dry_run:
        head = f"{report.would_stamp} would be stamped"
    else:
        head = f"{report.stamped} stamped"
    parts = [head, f"{report.failed} failed"]
    if report.already_correct:
        parts.append(f"{report.already_correct} already correct")
    if report.skipped_no_capture:
        parts.append(f"{report.skipped_no_capture} without capture time")
    if report.skipped_older:
        parts.append(f"{report.skipped_older} older than since")
    if report.ignored:
        parts.append(f"{report.ignored} ignored")
    return ", ".join(parts)


def timezone_for_config(config: AppConfig) -> str:
    """The template timezone capture times are interpreted in.

    The default template's timezone when present; otherwise the model default
    (``"Asia/Manila"``), matching the plan's motorcycle template.
    """
    template = config.templates.get(DEFAULT_TEMPLATE_NAME)
    if template is not None:
        return template.timezone
    return TemplateSettings().timezone


def _stamp_candidate(
    path: Path,
    capture: CaptureTime,
    birth: datetime | None,
    *,
    dry_run: bool,
    stamp: Callable[[Path, datetime], bool],
) -> StampResult:
    """Classify (and possibly stamp) one probed candidate."""
    when = capture.when
    if when is None:
        return StampResult(path, StampStatus.SKIPPED_NO_CAPTURE, raw=capture.raw)
    if birth is not None and _same_second(birth, _local_wall_clock(when)):
        return StampResult(path, StampStatus.ALREADY_CORRECT, when=when)
    if dry_run:
        return StampResult(path, StampStatus.WOULD_STAMP, when=when)
    if stamp(path, when):
        return StampResult(path, StampStatus.STAMPED, when=when)
    return StampResult(
        path, StampStatus.FAILED, when=when, error="the creation date was not set"
    )


def _local_wall_clock(when: datetime) -> datetime:
    """``when`` as naive local wall-clock time (how a birth time reads back)."""
    if when.tzinfo is None:
        return when
    return when.astimezone().replace(tzinfo=None)


def _same_second(left: datetime, right: datetime) -> bool:
    """Whether two wall-clock datetimes agree at second precision."""
    return left.replace(microsecond=0) == right.replace(microsecond=0)


def _iter_files(folder: Path) -> list[Path]:
    """Every file under ``folder`` in a sorted, symlink-free deterministic walk."""
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(folder, followlinks=False):
        dirnames[:] = sorted(
            name for name in dirnames if not (Path(dirpath) / name).is_symlink()
        )
        for name in sorted(filenames):
            candidate = Path(dirpath) / name
            if not candidate.is_symlink():
                found.append(candidate)
    return found


class _AttrList(ctypes.Structure):
    """C ``struct attrlist`` from ``<sys/attr.h>``."""

    _fields_ = [
        ("bitmapcount", ctypes.c_uint16),
        ("reserved", ctypes.c_uint16),
        ("commonattr", ctypes.c_uint32),
        ("volattr", ctypes.c_uint32),
        ("dirattr", ctypes.c_uint32),
        ("fileattr", ctypes.c_uint32),
        ("forkattr", ctypes.c_uint32),
    ]


class _TimeSpec(ctypes.Structure):
    """C ``struct timespec`` (the ``attrBuf`` payload for ``ATTR_CMN_CRTIME``)."""

    _fields_ = [("tv_sec", ctypes.c_long), ("tv_nsec", ctypes.c_long)]


_library: ctypes.CDLL | None = None


def _setattrlist_library() -> ctypes.CDLL | None:
    """Load libc once and type ``setattrlist``; ``None`` when unavailable."""
    global _library
    if _library is None:
        try:
            library = ctypes.CDLL(None, use_errno=True)
            library.setattrlist.restype = ctypes.c_int
            library.setattrlist.argtypes = [
                ctypes.c_char_p,
                ctypes.POINTER(_AttrList),
                ctypes.c_void_p,
                ctypes.c_size_t,
                ctypes.c_ulong,
            ]
        except (OSError, AttributeError):
            return None
        _library = library
    return _library
