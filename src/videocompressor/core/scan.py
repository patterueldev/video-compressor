"""Archive scanning, clip classification, and the dry-run report.

Classification combines the filesystem scan, the central manifest
(:mod:`videocompressor.core.manifest`), per-folder markers
(:mod:`videocompressor.core.markers`), and capture times from issue #2
(:mod:`videocompressor.core.capture_time`). A scan never writes anything except
the caller's report: markers and the manifest are read-only here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from videocompressor.core.capture_time import bucket_for, resolve_capture_time
from videocompressor.core.fingerprint import fingerprint_confirms, fingerprint_file
from videocompressor.core.manifest import Manifest
from videocompressor.core.markers import FolderMarker, MarkerClip, read_marker
from videocompressor.core.models import (
    Bucket,
    CaptureIssue,
    CaptureTime,
    CaptureTimeMode,
    Classification,
    ClipRecord,
    Fingerprint,
    JobState,
    MediaInfo,
    TemplateSettings,
)
from videocompressor.core.probe import ProbeError, probe_media

#: Media extensions discovered by a scan (matched case-insensitively).
MEDIA_SUFFIXES = frozenset({".mp4", ".mov"})

#: Default dry-run report file name (overridable with ``--report``).
DRY_RUN_REPORT_FILENAME = "report.json"

#: Manifest states where a worker is currently handling the clip.
_ACTIVE_STATES = frozenset({
    JobState.CLAIMED,
    JobState.COPIED,
    JobState.ENCODING,
    JobState.VALIDATED,
    JobState.TRANSFERRING,
    JobState.REPLACING,
})

_CAPTURE_NOTES = {
    CaptureIssue.MISSING_TIMESTAMP: "capture time missing",
    CaptureIssue.CONFLICTING_TIMESTAMPS: "capture time conflicts",
    CaptureIssue.UNREADABLE: "file unreadable",
    CaptureIssue.UNSUPPORTED: "unsupported file",
}


@dataclass(frozen=True)
class ClipInspection:
    """What an inspector learned about one candidate media file."""

    media: MediaInfo | None = None
    capture_time: CaptureTime | None = None
    error: str | None = None


#: Callable that inspects one media file (fixture-injected in tests).
Inspector = Callable[[Path], ClipInspection]


def probe_inspector(*, mode: CaptureTimeMode, timezone_name: str) -> Inspector:
    """Build the real inspector: ffprobe plus issue #2's capture-time resolver.

    Unreadable or unsupported files become inspection errors; missing or
    conflicting capture timestamps stay in the returned ``CaptureTime`` and the
    scan routes them to review. A missing ffprobe binary raises
    ``FFprobeUnavailableError`` out of the scan as an environment failure.
    """

    def inspect(path: Path) -> ClipInspection:
        try:
            media = probe_media(path)
        except ProbeError as error:
            return ClipInspection(error=str(error) or error.__class__.__name__)
        capture = resolve_capture_time(media, mode=mode, timezone=timezone_name)
        return ClipInspection(media=media, capture_time=capture)

    return inspect


@dataclass(frozen=True)
class ScanTarget:
    """One discovered media file with its archive-relative path."""

    path: Path
    relative_path: str


def discover_media(root: Path, folder: Path) -> list[ScanTarget]:
    """Recursively discover media files under ``folder`` inside ``root``.

    Raises ``ValueError`` when ``folder`` is not inside ``root``. Directories
    and files behind symlinks are skipped so a scan can never leave the
    archive root; results are returned in deterministic archive-path order.
    """
    root_resolved = root.resolve()
    folder_resolved = folder.resolve()
    if folder_resolved != root_resolved and root_resolved not in folder_resolved.parents:
        raise ValueError(f"folder {folder} is outside archive root {root}")
    targets: list[ScanTarget] = []
    for dirpath, dirnames, filenames in os.walk(folder_resolved, followlinks=False):
        dirnames[:] = sorted(
            name for name in dirnames if not (Path(dirpath) / name).is_symlink()
        )
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if path.suffix.lower() not in MEDIA_SUFFIXES or path.is_symlink():
                continue
            targets.append(
                ScanTarget(path=path, relative_path=path.relative_to(root_resolved).as_posix())
            )
    targets.sort(key=lambda target: target.relative_path)
    return targets


def settings_hash(settings: TemplateSettings) -> str:
    """Deterministic hash of the encoding template fields (marker/manifest key)."""
    payload = {
        "output_height": settings.output_height,
        "video_bitrate_kbps": settings.video_bitrate_kbps,
        "timezone": settings.timezone,
        "audio_codec": settings.audio_codec,
        "audio_bitrate_kbps": settings.audio_bitrate_kbps,
        "ffmpeg_preset": settings.ffmpeg_preset,
        "original_handling": str(settings.original_handling),
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    return digest[:16]


def estimate_output_bytes(
    duration_s: float | None, *, video_bitrate_kbps: int, audio_bitrate_kbps: int | None
) -> int | None:
    """Estimated output size from clip duration and configured bitrates."""
    if duration_s is None:
        return None
    total_kbps = video_bitrate_kbps + (audio_bitrate_kbps or 0)
    return round(duration_s * total_kbps * 1000 / 8)


def derive_root_id(root: Path) -> str:
    """Derive a stable archive-root id from a folder path.

    Used until roots are registered (issue #8); ``vc scan --root-id`` overrides
    it so later registration can pin the same identity.
    """
    resolved = root.resolve()
    slug = re.sub(r"[^a-z0-9]+", "-", resolved.name.lower()).strip("-") or "root"
    return f"{slug}-{hashlib.sha256(str(resolved).encode('utf-8')).hexdigest()[:8]}"


@dataclass(frozen=True)
class ScanRow:
    """One classified clip in a dry-run report."""

    relative_path: str
    classification: Classification
    size_bytes: int | None = None
    mtime_ns: int | None = None
    digest: str | None = None
    capture_time: CaptureTime | None = None
    bucket: Bucket | None = None
    video_bitrate_kbps: int | None = None
    audio_bitrate_kbps: int | None = None
    estimated_bytes: int | None = None
    note: str | None = None


@dataclass(frozen=True)
class ScanReport:
    """Result of one dry run over a footage folder inside an archive root."""

    root_id: str
    root_path: Path
    folder: Path
    folder_relative: str
    template_id: str
    settings_hash: str
    generated_at: datetime
    rows: tuple[ScanRow, ...]

    def counts(self) -> dict[str, int]:
        """Clip counts per classification, in model order."""
        return {
            str(kind): sum(1 for row in self.rows if row.classification is kind)
            for kind in Classification
        }

    def estimated_bytes(self) -> int:
        """Total estimated output size of the clips this run would process."""
        return sum(row.estimated_bytes or 0 for row in self.rows)


def scan_folder(
    *,
    root_id: str,
    root: Path,
    folder: Path,
    template_id: str,
    settings: TemplateSettings,
    capture_mode: CaptureTimeMode = CaptureTimeMode.LOCAL_WALL_CLOCK,
    manifest: Manifest | None = None,
    inspector: Inspector | None = None,
) -> ScanReport:
    """Classify every media file under ``folder`` without writing anything."""
    if inspector is None:
        inspector = probe_inspector(mode=capture_mode, timezone_name=settings.timezone)
    targets = discover_media(root, folder)
    root_resolved = root.resolve()
    folder_resolved = folder.resolve()
    if folder_resolved == root_resolved:
        folder_relative = "."
    else:
        folder_relative = folder_resolved.relative_to(root_resolved).as_posix()

    markers: dict[Path, FolderMarker | None] = {}
    rows: list[ScanRow] = []
    for target in targets:
        marker_folder = target.path.parent
        if marker_folder not in markers:
            markers[marker_folder] = read_marker(marker_folder)
        marker = markers[marker_folder]
        marker_clip = marker.clips.get(target.path.name) if marker is not None else None
        record = (
            manifest.get_clip(root_id, target.relative_path) if manifest is not None else None
        )

        try:
            current = fingerprint_file(target.path)
        except OSError as error:
            rows.append(
                ScanRow(
                    relative_path=target.relative_path,
                    classification=Classification.REVIEW,
                    note=f"unreadable: {error}",
                )
            )
            continue

        classification, note = _decide(current=current, record=record, marker_clip=marker_clip)
        capture: CaptureTime | None = None
        bucket: str | None = None
        video_bitrate_kbps: int | None = None
        audio_bitrate_kbps: int | None = None
        estimated_bytes: int | None = None

        if classification is Classification.NEW:
            inspection = inspector(target.path)
            if inspection.error is not None:
                classification = Classification.REVIEW
                note = f"probe failed: {inspection.error}"
            elif (
                inspection.capture_time is None
                or inspection.capture_time.when is None
                or inspection.capture_time.issue is not None
            ):
                classification = Classification.REVIEW
                note = _capture_note(inspection.capture_time)
            else:
                capture = inspection.capture_time
                assert capture.when is not None
                bucket = bucket_for(capture.when, timezone=settings.timezone)
                video_bitrate_kbps = settings.video_bitrate_kbps
                media = inspection.media
                has_audio = media is not None and any(
                    stream.codec_type == "audio" for stream in media.streams
                )
                audio_bitrate_kbps = settings.audio_bitrate_kbps if has_audio else None
                estimated_bytes = estimate_output_bytes(
                    media.duration_s if media is not None else None,
                    video_bitrate_kbps=video_bitrate_kbps,
                    audio_bitrate_kbps=audio_bitrate_kbps,
                )
                if not has_audio:
                    note = "no audio stream"

        rows.append(
            ScanRow(
                relative_path=target.relative_path,
                classification=classification,
                size_bytes=current.size_bytes,
                mtime_ns=current.mtime_ns,
                digest=current.digest,
                capture_time=capture,
                bucket=bucket,
                video_bitrate_kbps=video_bitrate_kbps,
                audio_bitrate_kbps=audio_bitrate_kbps,
                estimated_bytes=estimated_bytes,
                note=note,
            )
        )

    return ScanReport(
        root_id=root_id,
        root_path=root_resolved,
        folder=folder_resolved,
        folder_relative=folder_relative,
        template_id=template_id,
        settings_hash=settings_hash(settings),
        generated_at=datetime.now(UTC),
        rows=tuple(rows),
    )


def report_to_dict(report: ScanReport) -> dict[str, object]:
    """Serialize a scan report for ``report.json``."""
    return {
        "root_id": report.root_id,
        "root": str(report.root_path),
        "folder": report.folder_relative,
        "template_id": report.template_id,
        "settings_hash": report.settings_hash,
        "generated_at": report.generated_at.isoformat(),
        "summary": {
            "total": len(report.rows),
            **report.counts(),
            "estimated_bytes": report.estimated_bytes(),
        },
        "clips": [
            {
                "relative_path": row.relative_path,
                "classification": str(row.classification),
                "size_bytes": row.size_bytes,
                "mtime_ns": row.mtime_ns,
                "digest": row.digest,
                "capture_time": (
                    row.capture_time.when.isoformat()
                    if row.capture_time is not None and row.capture_time.when is not None
                    else None
                ),
                "bucket": row.bucket.value if row.bucket is not None else None,
                "video_bitrate_kbps": row.video_bitrate_kbps,
                "audio_bitrate_kbps": row.audio_bitrate_kbps,
                "estimated_bytes": row.estimated_bytes,
                "note": row.note,
            }
            for row in report.rows
        ],
    }


def write_report(report: ScanReport, path: Path) -> Path:
    """Write the dry-run report as JSON and return its path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report_to_dict(report), indent=2) + "\n", encoding="utf-8")
    return path


def render_table(report: ScanReport) -> str:
    """Render the human-readable dry-run table for the console."""
    header = ("clip", "capture time", "bucket", "state", "bitrate", "est. size", "note")
    table = [_row_cells(row) for row in report.rows]
    widths = [len(title) for title in header]
    for cells in table:
        for index, cell in enumerate(cells):
            widths[index] = max(widths[index], len(cell))
    lines = [
        f"Archive root: {report.root_path}  (root id {report.root_id})",
        f"Folder:       {report.folder_relative}",
        f"Template:     {report.template_id}  (settings {report.settings_hash})",
        "",
        _format_cells(header, widths),
        _format_cells(tuple("-" * width for width in widths), widths),
    ]
    lines.extend(_format_cells(cells, widths) for cells in table)
    lines.append("")
    counts = report.counts()
    summary = ", ".join(f"{count} {name}" for name, count in counts.items() if count)
    lines.append(
        f"Total {len(report.rows)} clips ({summary or 'nothing to do'}); "
        f"estimated output {_human_bytes(report.estimated_bytes())}"
    )
    return "\n".join(lines) + "\n"


def _decide(
    *,
    current: Fingerprint,
    record: ClipRecord | None,
    marker_clip: MarkerClip | None,
) -> tuple[Classification, str | None]:
    """Classify one clip from its fingerprints, manifest record, and marker.

    Completion is trusted from either source of truth as long as the recorded
    output fingerprint (size + digest) still matches the file, so losing one of
    the two never re-encodes a completed clip and a template change never
    invalidates one. Any recorded completion that no longer matches is
    ``changed`` (review-worthy), never a silent replace.
    """
    recorded_outputs: list[Fingerprint] = []
    if record is not None and record.state is JobState.COMPLETE:
        if record.output is None:
            return (
                Classification.REVIEW,
                "completed in the manifest but without an output fingerprint",
            )
        recorded_outputs.append(record.output)
    if marker_clip is not None:
        recorded_outputs.append(marker_clip.output)
    if recorded_outputs:
        if any(fingerprint_confirms(recorded, current) for recorded in recorded_outputs):
            return Classification.COMPLETE, None
        return Classification.CHANGED, "file changed since it was completed"
    if record is not None:
        if record.state is JobState.REVIEW:
            return Classification.REVIEW, _detail("parked in review", record.error)
        if record.state is JobState.FAILED:
            return Classification.REVIEW, _detail("previous failure", record.error)
        if record.state in _ACTIVE_STATES:
            return Classification.REVIEW, _detail(f"in progress ({record.state})", record.worker_id)
        if not fingerprint_confirms(record.source, current):
            return Classification.CHANGED, "source changed since it was recorded"
    return Classification.NEW, None


def _detail(label: str, detail: str | None) -> str:
    return f"{label}: {detail}" if detail else label


def _capture_note(capture: CaptureTime | None) -> str:
    if capture is None or capture.issue is None:
        return "capture time unavailable"
    note = _CAPTURE_NOTES.get(capture.issue, str(capture.issue))
    return f"{note}: {capture.raw}" if capture.raw else note


def _row_cells(row: ScanRow) -> tuple[str, ...]:
    when = row.capture_time.when if row.capture_time is not None else None
    return (
        row.relative_path,
        when.isoformat(sep=" ") if when is not None else "-",
        row.bucket.value if row.bucket is not None else "-",
        str(row.classification),
        f"{row.video_bitrate_kbps} kbps" if row.video_bitrate_kbps is not None else "-",
        _human_bytes(row.estimated_bytes),
        row.note or "",
    )


def _format_cells(cells: tuple[str, ...], widths: list[int]) -> str:
    return "  ".join(cell.ljust(width) for cell, width in zip(cells, widths, strict=True)).rstrip()


def _human_bytes(value: int | None) -> str:
    if value is None:
        return "-"
    if value < 1_000_000:
        return f"{value / 1024:.0f} KB"
    return f"{value / 1_000_000:.1f} MB"
