"""GUI scan service: classification via scan_folder plus the manifest policy (issue #8)."""

from __future__ import annotations

from pathlib import Path

import pytest

from videocompressor.core.capture_time import resolve_capture_time
from videocompressor.core.fingerprint import fingerprint_file
from videocompressor.core.manifest import Manifest
from videocompressor.core.models import (
    ArchiveRoot,
    Bucket,
    CaptureTimeMode,
    Classification,
    ClipRecord,
    JobState,
    MediaInfo,
    MediaStream,
    TemplateSettings,
)
from videocompressor.core.probe import FFprobeUnavailableError
from videocompressor.core.scan import ClipInspection, estimate_output_bytes
from videocompressor.gui.scan_service import (
    ScanManifestError,
    ScanRequest,
    describe_scan_error,
    run_scan,
)

EVENING_FACE = "2026-06-14T17:41:00"  # naive: local wall clock in Asia/Manila


def _media(creation_time: str | None = EVENING_FACE, duration_s: float = 900.0) -> MediaInfo:
    return MediaInfo(
        path=Path("ignored"),
        format_name="mov,mp4",
        duration_s=duration_s,
        size_bytes=4_000_000,
        bit_rate=9_000_000,
        format_tags={"creation_time": creation_time} if creation_time else {},
        streams=(
            MediaStream(index=0, codec_type="video", codec_name="hevc", width=1920, height=1080),
            MediaStream(index=1, codec_type="audio", codec_name="aac", channels=2),
        ),
    )


def _inspector(media_by_name: dict[str, MediaInfo]):
    def inspect(path: Path) -> ClipInspection:
        media = media_by_name.get(path.name)
        if media is None:
            return ClipInspection(error="unreadable fixture")
        capture = resolve_capture_time(
            media, mode=CaptureTimeMode.LOCAL_WALL_CLOCK, timezone="Asia/Manila"
        )
        return ClipInspection(media=media, capture_time=capture)

    return inspect


@pytest.fixture
def archive(tmp_path: Path):
    root = tmp_path / "archive"
    folder = root / "2026-06-Ride"
    folder.mkdir(parents=True)
    clip = folder / "GS010001.MP4"
    clip.write_bytes(b"fixture bytes" * 512)
    return root, folder


def _request(root: Path, folder: Path, manifest_path: Path | None = None) -> ScanRequest:
    return ScanRequest(
        root=ArchiveRoot(id="camera", name="Camera", path=root),
        folder=folder,
        template_id="motorcycle",
        settings=TemplateSettings(),
        capture_mode=CaptureTimeMode.LOCAL_WALL_CLOCK,
        manifest_path=manifest_path,
    )


def test_scan_classifies_new_clip_with_bucket_and_estimate(archive):
    root, folder = archive
    report = run_scan(
        _request(root, folder),
        inspector=_inspector({"GS010001.MP4": _media()}),
    )

    assert report.root_id == "camera"
    assert report.folder_relative == "2026-06-Ride"
    (row,) = report.rows
    assert row.classification is Classification.NEW
    assert row.bucket is Bucket.EVENING
    assert row.video_bitrate_kbps == 800
    assert row.capture_time is not None
    assert row.capture_time.when.isoformat() == "2026-06-14T17:41:00+08:00"
    expected = estimate_output_bytes(900.0, video_bitrate_kbps=800, audio_bitrate_kbps=96)
    assert row.estimated_bytes == expected


def test_scan_routes_probe_failures_and_missing_timestamps_to_review(archive):
    root, folder = archive
    report = run_scan(
        _request(root, folder),
        inspector=_inspector({"GS010001.MP4": _media(creation_time=None)}),
    )
    (row,) = report.rows
    assert row.classification is Classification.REVIEW
    assert "capture time missing" in (row.note or "")

    report = run_scan(
        _request(root, folder),
        inspector=_inspector({}),
    )
    (row,) = report.rows
    assert row.classification is Classification.REVIEW
    assert (row.note or "").startswith("probe failed")


def test_scan_without_manifest_file_still_classifies(archive, tmp_path):
    root, folder = archive
    report = run_scan(
        _request(root, folder, tmp_path / "missing" / "manifest.sqlite"),
        inspector=_inspector({"GS010001.MP4": _media()}),
    )
    assert report.rows[0].classification is Classification.NEW


def test_scan_uses_completed_manifest_read_only(archive, tmp_path):
    root, folder = archive
    manifest_path = tmp_path / "state" / "manifest.sqlite"
    manifest_path.parent.mkdir()
    source = fingerprint_file(folder / "GS010001.MP4")
    record = ClipRecord(
        root_id="camera",
        relative_path="2026-06-Ride/GS010001.MP4",
        source=source,
        state=JobState.COMPLETE,
        classification=Classification.COMPLETE,
        output=source,
    )
    with Manifest.open(manifest_path) as manifest:
        manifest.upsert_clip(record)
        saved = manifest.get_clip("camera", "2026-06-Ride/GS010001.MP4")
    before = manifest_path.read_bytes()

    def exploding_inspector(path: Path) -> ClipInspection:
        raise AssertionError("complete clips must not be probed again")

    report = run_scan(
        _request(root, folder, manifest_path),
        inspector=exploding_inspector,
    )
    assert report.rows[0].classification is Classification.COMPLETE
    assert manifest_path.read_bytes() == before
    assert not list(root.rglob("manifest.json"))
    with Manifest.open(manifest_path, create=False) as reopened:
        assert reopened.get_clip("camera", "2026-06-Ride/GS010001.MP4") == saved


def test_scan_reports_broken_manifest(archive, tmp_path):
    root, folder = archive
    manifest_path = tmp_path / "manifest.sqlite"
    manifest_path.write_bytes(b"this is not sqlite")
    with pytest.raises(ScanManifestError, match="2026-06-Ride|manifest"):
        run_scan(_request(root, folder, manifest_path))


def test_scan_rejects_folder_outside_root(archive, tmp_path):
    root, _folder = archive
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    with pytest.raises(ValueError, match="outside archive root"):
        run_scan(_request(root, outside))


def test_describe_scan_error_messages():
    assert (
        describe_scan_error(FFprobeUnavailableError("ffprobe missing"))
        == "ffprobe was not found; install FFmpeg and try again"
    )
    assert describe_scan_error(ScanManifestError("cannot read manifest x")) == (
        "cannot read manifest x"
    )
    assert describe_scan_error(ValueError("folder outside archive root")) == (
        "folder outside archive root"
    )
    assert describe_scan_error(OSError("disk error")) == "disk error"
    assert describe_scan_error(RuntimeError("boom")) == "unexpected error: RuntimeError('boom')"
