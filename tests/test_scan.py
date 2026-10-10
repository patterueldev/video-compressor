"""Scan classification: fixture trees, marker/manifest matrix, estimates."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from videocompressor.core.fingerprint import fingerprint_file
from videocompressor.core.manifest import Manifest
from videocompressor.core.markers import FolderMarker, MarkerClip, write_marker
from videocompressor.core.models import (
    CaptureIssue,
    CaptureTime,
    CaptureTimeMode,
    Classification,
    ClipRecord,
    Fingerprint,
    JobState,
    MediaInfo,
    MediaStream,
    TemplateSettings,
)
from videocompressor.core.scan import (
    ClipInspection,
    derive_root_id,
    discover_media,
    estimate_output_bytes,
    probe_inspector,
    scan_folder,
    settings_hash,
)

ROOT_ID = "archive"
PHT = timezone(timedelta(hours=8))
EVENING_WHEN = datetime(2024, 6, 14, 17, 41, tzinfo=PHT)
DAYTIME_WHEN = datetime(2024, 6, 14, 9, 5, tzinfo=PHT)


def _write_file(path: Path, size: int = 8192, seed: int = 0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.write(bytes((i + seed) % 251 for i in range(min(size, 4096))))
        if size > 4096:
            handle.truncate(size)
    return path


def _media(duration_s: float = 60.0, *, audio: bool = True) -> MediaInfo:
    streams = [MediaStream(index=0, codec_type="video", codec_name="hevc", width=1920, height=1080)]
    if audio:
        streams.append(MediaStream(index=1, codec_type="audio", codec_name="aac", channels=2))
    return MediaInfo(
        path=Path("ignored"),
        format_name="mov,mp4",
        duration_s=duration_s,
        size_bytes=8192,
        bit_rate=9_000_000,
        streams=tuple(streams),
    )


def _capture(when: datetime | None) -> CaptureTime:
    return CaptureTime(
        when=when,
        raw=when.isoformat() if when else None,
        source="format",
        mode=CaptureTimeMode.UTC,
    )


def _inspector(
    table: dict[str, ClipInspection],
) -> tuple[Callable[[Path], ClipInspection], list[str]]:
    calls: list[str] = []

    def inspect(path: Path) -> ClipInspection:
        calls.append(path.name)
        if path.name not in table:
            raise AssertionError(f"unexpected inspection of {path.name}")
        return table[path.name]

    return inspect, calls


def _never_called(path: Path) -> ClipInspection:
    raise AssertionError(f"unexpected inspection of {path}")


def _record(
    relative_path: str,
    *,
    state: JobState,
    source: Fingerprint,
    output: Fingerprint | None = None,
) -> ClipRecord:
    return ClipRecord(
        root_id=ROOT_ID,
        relative_path=relative_path,
        source=source,
        state=state,
        classification=Classification.NEW,
        output=output,
    )


def test_discover_media_is_recursive_case_insensitive_and_sorted(tmp_path: Path):
    root = tmp_path / "archive"
    folder = root / "ride"
    _write_file(folder / "c.MP4")
    _write_file(folder / "a.mp4")
    _write_file(folder / "B.MOV")
    _write_file(folder / "notes.txt")
    _write_file(folder / "sub" / "d.MOV")
    _write_file(folder / "sub" / "e.MP4")

    targets = discover_media(root, folder)

    assert [target.relative_path for target in targets] == [
        "ride/B.MOV",
        "ride/a.mp4",
        "ride/c.MP4",
        "ride/sub/d.MOV",
        "ride/sub/e.MP4",
    ]
    assert all(target.path.is_file() for target in targets)


def test_discover_media_skips_symlinked_files_and_directories(tmp_path: Path):
    root = tmp_path / "archive"
    folder = root / "ride"
    _write_file(folder / "real.MP4")
    outside_file = _write_file(tmp_path / "outside.MOV")
    outside_dir = _write_file(tmp_path / "elsewhere" / "hidden.MP4").parent
    (folder / "link.MOV").symlink_to(outside_file)
    (folder / "linked-dir").symlink_to(outside_dir)

    targets = discover_media(root, folder)

    assert [target.relative_path for target in targets] == ["ride/real.MP4"]


def test_discover_media_rejects_a_folder_outside_the_root(tmp_path: Path):
    root = tmp_path / "archive"
    other = tmp_path / "other"
    root.mkdir()
    other.mkdir()

    with pytest.raises(ValueError, match="outside archive root"):
        discover_media(root, other)


def test_discover_media_accepts_the_root_itself(tmp_path: Path):
    root = tmp_path / "archive"
    _write_file(root / "clip.MP4")

    assert [target.relative_path for target in discover_media(root, root)] == ["clip.MP4"]


def test_scan_classifies_a_fixture_tree(tmp_path: Path):
    root = tmp_path / "archive"
    folder = root / "2024-06-Ride"
    done = folder / "GS_DONE.MP4"
    manifest_done = folder / "GS_MANIFEST_DONE.MP4"
    changed = folder / "GS_CHANGED.MP4"
    new = folder / "GS_NEW.MP4"
    review = folder / "GS_REVIEW.MP4"
    no_audio = folder / "GS_NOAUDIO.MP4"
    for path in (done, manifest_done, changed, new, review, no_audio):
        _write_file(path)

    write_marker(
        folder,
        FolderMarker(
            template_id="motorcycle",
            settings_hash="deadbeef",
            clips={
                "GS_DONE.MP4": MarkerClip(output=fingerprint_file(done)),
                "GS_CHANGED.MP4": MarkerClip(
                    output=Fingerprint(size_bytes=1, mtime_ns=1, digest="0" * 64)
                ),
            },
        ),
    )

    database = tmp_path / "state" / "manifest.sqlite"
    with Manifest.open(database) as manifest:
        manifest.upsert_clip(
            _record(
                "2024-06-Ride/GS_MANIFEST_DONE.MP4",
                state=JobState.COMPLETE,
                source=Fingerprint(size_bytes=1, mtime_ns=1, digest="1" * 64),
                output=fingerprint_file(manifest_done),
            )
        )
        manifest.upsert_clip(
            _record(
                "2024-06-Ride/GS_NEW.MP4",
                state=JobState.QUEUED,
                source=fingerprint_file(new),
            )
        )

        inspector, calls = _inspector(
            {
                "GS_NEW.MP4": ClipInspection(
                    media=_media(120.0), capture_time=_capture(DAYTIME_WHEN)
                ),
                "GS_REVIEW.MP4": ClipInspection(
                    media=_media(),
                    capture_time=CaptureTime(
                        when=None,
                        raw=None,
                        source="format",
                        issue=CaptureIssue.CONFLICTING_TIMESTAMPS,
                    ),
                ),
                "GS_NOAUDIO.MP4": ClipInspection(
                    media=_media(60.0, audio=False), capture_time=_capture(EVENING_WHEN)
                ),
            }
        )

        report = scan_folder(
            root_id=ROOT_ID,
            root=root,
            folder=folder,
            template_id="motorcycle",
            settings=TemplateSettings(),
            manifest=manifest,
            inspector=inspector,
        )

    by_path = {row.relative_path: row for row in report.rows}
    expected_paths = {
        f"2024-06-Ride/{path.name}"
        for path in (done, manifest_done, changed, new, review, no_audio)
    }
    assert set(by_path) == expected_paths
    assert by_path["2024-06-Ride/GS_DONE.MP4"].classification is Classification.COMPLETE
    assert by_path["2024-06-Ride/GS_MANIFEST_DONE.MP4"].classification is Classification.COMPLETE
    assert by_path["2024-06-Ride/GS_CHANGED.MP4"].classification is Classification.CHANGED
    assert by_path["2024-06-Ride/GS_CHANGED.MP4"].note == "file changed since it was completed"
    assert by_path["2024-06-Ride/GS_REVIEW.MP4"].classification is Classification.REVIEW
    assert by_path["2024-06-Ride/GS_REVIEW.MP4"].note == "capture time conflicts"

    new_row = by_path["2024-06-Ride/GS_NEW.MP4"]
    assert new_row.classification is Classification.NEW
    assert new_row.bucket == "daytime"
    assert new_row.video_bitrate_kbps == 800
    assert new_row.audio_bitrate_kbps == 96
    assert new_row.estimated_bytes == 120 * 896_000 // 8
    assert new_row.capture_time is not None and new_row.capture_time.when == DAYTIME_WHEN

    no_audio_row = by_path["2024-06-Ride/GS_NOAUDIO.MP4"]
    assert no_audio_row.classification is Classification.NEW
    assert no_audio_row.bucket == "evening"
    assert no_audio_row.audio_bitrate_kbps is None
    assert no_audio_row.estimated_bytes == 60 * 800_000 // 8
    assert no_audio_row.note == "no audio stream"

    # Completed and changed clips are never probed.
    assert calls == ["GS_NEW.MP4", "GS_NOAUDIO.MP4", "GS_REVIEW.MP4"]

    assert report.counts() == {"new": 2, "complete": 2, "changed": 1, "review": 1}
    assert report.folder_relative == "2024-06-Ride"
    assert report.estimated_bytes() == 120 * 896_000 // 8 + 60 * 800_000 // 8


def test_completion_survives_a_lost_marker_when_the_manifest_is_present(tmp_path: Path):
    root = tmp_path / "archive"
    folder = root / "ride"
    clip = _write_file(folder / "GS010001.MP4")

    with Manifest.open(tmp_path / "manifest.sqlite") as manifest:
        manifest.upsert_clip(
            _record(
                "ride/GS010001.MP4",
                state=JobState.COMPLETE,
                source=Fingerprint(size_bytes=9, mtime_ns=9, digest="9" * 64),
                output=fingerprint_file(clip),
            )
        )
        report = scan_folder(
            root_id=ROOT_ID,
            root=root,
            folder=folder,
            template_id="motorcycle",
            settings=TemplateSettings(),
            manifest=manifest,
            inspector=_never_called,
        )

    assert report.rows[0].classification is Classification.COMPLETE


def test_completion_survives_a_lost_manifest_when_the_marker_is_present(tmp_path: Path):
    root = tmp_path / "archive"
    folder = root / "ride"
    clip = _write_file(folder / "GS010001.MP4")
    write_marker(
        folder,
        FolderMarker(
            template_id="motorcycle",
            settings_hash="deadbeef",
            clips={"GS010001.MP4": MarkerClip(output=fingerprint_file(clip))},
        ),
    )

    report = scan_folder(
        root_id=ROOT_ID,
        root=root,
        folder=folder,
        template_id="motorcycle",
        settings=TemplateSettings(),
        inspector=_never_called,
    )

    assert report.rows[0].classification is Classification.COMPLETE


def test_a_template_change_does_not_invalidate_completed_clips(tmp_path: Path):
    root = tmp_path / "archive"
    folder = root / "ride"
    clip = _write_file(folder / "GS010001.MP4")
    changed_settings = TemplateSettings(daytime_video_bitrate_kbps=1200)
    assert settings_hash(changed_settings) != "old-settings-hash"
    write_marker(
        folder,
        FolderMarker(
            template_id="motorcycle",
            settings_hash="old-settings-hash",
            clips={"GS010001.MP4": MarkerClip(output=fingerprint_file(clip))},
        ),
    )

    report = scan_folder(
        root_id=ROOT_ID,
        root=root,
        folder=folder,
        template_id="motorcycle",
        settings=changed_settings,
        inspector=_never_called,
    )

    assert report.rows[0].classification is Classification.COMPLETE


def test_a_recorded_source_change_is_flagged_changed(tmp_path: Path):
    root = tmp_path / "archive"
    folder = root / "ride"
    clip = _write_file(folder / "GS010001.MP4")

    with Manifest.open(tmp_path / "manifest.sqlite") as manifest:
        manifest.upsert_clip(
            _record(
                "ride/GS010001.MP4",
                state=JobState.QUEUED,
                source=Fingerprint(size_bytes=1, mtime_ns=1, digest="0" * 64),
            )
        )
        report = scan_folder(
            root_id=ROOT_ID,
            root=root,
            folder=folder,
            template_id="motorcycle",
            settings=TemplateSettings(),
            manifest=manifest,
            inspector=_never_called,
        )

    assert report.rows[0].classification is Classification.CHANGED
    assert report.rows[0].note == "source changed since it was recorded"
    assert report.rows[0].size_bytes == clip.stat().st_size


def test_probe_inspector_reports_the_issue_2_stub_as_an_error(tmp_path: Path):
    clip = _write_file(tmp_path / "clip.MP4")

    inspection = probe_inspector()(clip)

    assert inspection.media is None
    assert inspection.error is not None and "issue #2" in inspection.error


def test_scan_marks_everything_review_without_capture_time_support(tmp_path: Path):
    root = tmp_path / "archive"
    folder = root / "ride"
    _write_file(folder / "GS010001.MP4")

    report = scan_folder(
        root_id=ROOT_ID,
        root=root,
        folder=folder,
        template_id="motorcycle",
        settings=TemplateSettings(),
    )

    assert report.rows[0].classification is Classification.REVIEW
    assert report.rows[0].note is not None and "issue #2" in report.rows[0].note


def test_estimate_output_bytes_from_duration_and_bitrates():
    assert estimate_output_bytes(60.0, video_bitrate_kbps=800, audio_bitrate_kbps=96) == 6_720_000
    assert estimate_output_bytes(60.0, video_bitrate_kbps=800, audio_bitrate_kbps=None) == 6_000_000
    assert estimate_output_bytes(None, video_bitrate_kbps=800, audio_bitrate_kbps=96) is None


def test_settings_hash_is_stable_and_sensitive():
    assert settings_hash(TemplateSettings()) == settings_hash(TemplateSettings())
    assert settings_hash(TemplateSettings()) != settings_hash(
        TemplateSettings(daytime_video_bitrate_kbps=999)
    )


def test_derive_root_id_is_stable_and_path_specific():
    assert derive_root_id(Path("/tmp/GoPro Archive")) == derive_root_id(Path("/tmp/GoPro Archive"))
    assert derive_root_id(Path("/tmp/GoPro Archive")) != derive_root_id(Path("/tmp/Other Archive"))
