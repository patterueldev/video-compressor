"""Creation-date stamping: seams, idempotence, since filter, and the real round trip.

The fast tests inject every seam (probe, stamp, birth-time reader), so they need
neither ffmpeg nor macOS. The round-trip test is gated to macOS, the only
supported platform.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from videocompressor.core.date_stamp import (
    StampStatus,
    read_creation_time,
    stamp_creation_time,
    summarize,
    supported,
    sweep_folder,
)
from videocompressor.core.models import CaptureTimeMode, MediaInfo, MediaStream

MODE = CaptureTimeMode.LOCAL_WALL_CLOCK
TZ = "Asia/Manila"

#: The instant the fake probes resolve: 09:05 in the template timezone.
CAPTURE_WHEN = datetime(2024, 6, 14, 9, 5, tzinfo=ZoneInfo(TZ))


def _as_birth_time(when: datetime) -> datetime:
    """The naive local wall-clock value ``when`` reads back as on this runner.

    Birth times are instants, so on a machine whose timezone is not the
    template's the stamped value reads back as the local conversion rather
    than the face value (CI runs in UTC). Seeding the fake ``read_birth``
    through this helper keeps the classification tests timezone-independent.
    """
    return when.astimezone().replace(tzinfo=None)


def _write(path: Path, payload: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _media(creation_time: str | None = "2024-06-14T09:05:00.000000Z") -> MediaInfo:
    tags = {"creation_time": creation_time} if creation_time is not None else {}
    return MediaInfo(
        path=Path("ignored"),
        format_tags=tags,
        streams=(MediaStream(index=0, codec_type="video", codec_name="hevc"),),
    )


class ProbeSpy:
    """Fake probe that records calls and serves one ``MediaInfo`` per file name."""

    def __init__(self, table: dict[str, MediaInfo | Exception] | None = None) -> None:
        self.calls: list[Path] = []
        self.table = table or {}

    def __call__(self, path: Path) -> MediaInfo:
        self.calls.append(path)
        value = self.table.get(path.name)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise AssertionError(f"unexpected probe of {path.name}")
        return value


class StampSpy:
    """Fake stamp seam: records calls, returns per-file results."""

    def __init__(self, results: dict[str, bool] | None = None) -> None:
        self.calls: list[tuple[Path, datetime]] = []
        self.results = results or {}

    def __call__(self, path: Path, when: datetime) -> bool:
        self.calls.append((path, when))
        return self.results.get(path.name, True)


def _births(table: dict[str, datetime]) -> object:
    return lambda path: table.get(path.name)


def test_sweep_stamps_the_resolved_capture_time(tmp_path: Path):
    video = _write(tmp_path / "A.MP4")
    probe = ProbeSpy({video.name: _media()})
    stamp = StampSpy()

    report = sweep_folder(
        tmp_path, mode=MODE, timezone=TZ, probe=probe, stamp=stamp, read_birth=_births({})
    )

    assert report.scanned == 1
    assert report.stamped == 1
    assert [result.status for result in report.results] == [StampStatus.STAMPED]
    (path, when) = stamp.calls[0]
    assert path == video
    assert when == CAPTURE_WHEN
    assert report.results[0].when == when


def test_second_sweep_reports_already_correct_without_stamping(tmp_path: Path):
    video = _write(tmp_path / "A.MP4")
    probe = ProbeSpy({video.name: _media()})
    stamp = StampSpy()
    # A stamped file reads back as the local wall clock of the instant; the
    # sub-second noise proves the comparison is at second precision.
    births = {video.name: _as_birth_time(CAPTURE_WHEN).replace(microsecond=999999)}

    report = sweep_folder(
        tmp_path, mode=MODE, timezone=TZ, probe=probe, stamp=stamp, read_birth=_births(births)
    )

    assert report.already_correct == 1
    assert report.stamped == 0
    assert stamp.calls == []
    assert report.results[0].status is StampStatus.ALREADY_CORRECT


def test_unresolved_capture_time_keeps_the_raw_value(tmp_path: Path):
    video = _write(tmp_path / "A.MP4")
    probe = ProbeSpy({video.name: _media("not-a-timestamp")})
    stamp = StampSpy()

    report = sweep_folder(
        tmp_path, mode=MODE, timezone=TZ, probe=probe, stamp=stamp, read_birth=_births({})
    )

    assert report.skipped_no_capture == 1
    assert report.results[0].raw == "not-a-timestamp"
    assert stamp.calls == []


def test_since_filter_skips_older_files_without_probing(tmp_path: Path):
    old = _write(tmp_path / "OLD.MP4")
    new = _write(tmp_path / "NEW.MP4")
    probe = ProbeSpy({new.name: _media()})
    stamp = StampSpy()
    births = {old.name: datetime(2020, 1, 1, 12, 0), new.name: _as_birth_time(CAPTURE_WHEN)}

    report = sweep_folder(
        tmp_path,
        mode=MODE,
        timezone=TZ,
        since=datetime(2024, 1, 1),
        probe=probe,
        stamp=stamp,
        read_birth=_births(births),
    )

    statuses = {result.path.name: result.status for result in report.results}
    assert statuses == {old.name: StampStatus.SKIPPED_OLDER, new.name: StampStatus.ALREADY_CORRECT}
    assert report.skipped_older == 1
    assert [path.name for path in probe.calls] == [new.name]


def test_since_filter_still_probes_when_the_birth_time_is_unknown(tmp_path: Path):
    video = _write(tmp_path / "A.MP4")
    probe = ProbeSpy({video.name: _media()})
    stamp = StampSpy()

    report = sweep_folder(
        tmp_path,
        mode=MODE,
        timezone=TZ,
        since=datetime(2024, 1, 1),
        probe=probe,
        stamp=stamp,
        read_birth=_births({}),
    )

    assert [path.name for path in probe.calls] == [video.name]
    assert report.stamped == 1


def test_dry_run_classifies_without_mutating(tmp_path: Path):
    video = _write(tmp_path / "A.MP4")
    probe = ProbeSpy({video.name: _media()})
    stamp = StampSpy()

    report = sweep_folder(
        tmp_path,
        mode=MODE,
        timezone=TZ,
        dry_run=True,
        probe=probe,
        stamp=stamp,
        read_birth=_births({}),
    )

    assert report.dry_run is True
    assert report.would_stamp == 1
    assert report.stamped == 0
    assert report.results[0].status is StampStatus.WOULD_STAMP
    assert stamp.calls == []


def test_a_failed_stamp_is_counted_and_the_sweep_continues(tmp_path: Path):
    first = _write(tmp_path / "A.MP4")
    second = _write(tmp_path / "B.MP4")
    probe = ProbeSpy({first.name: _media(), second.name: _media()})
    stamp = StampSpy({first.name: False})

    report = sweep_folder(
        tmp_path, mode=MODE, timezone=TZ, probe=probe, stamp=stamp, read_birth=_births({})
    )

    statuses = {result.path.name: result.status for result in report.results}
    assert statuses == {first.name: StampStatus.FAILED, second.name: StampStatus.STAMPED}
    assert report.failed == 1
    failed = next(result for result in report.results if result.status is StampStatus.FAILED)
    assert failed.error and "not set" in failed.error


def test_a_failed_probe_is_counted_and_the_sweep_continues(tmp_path: Path):
    broken = _write(tmp_path / "BROKEN.MP4")
    good = _write(tmp_path / "GOOD.MP4")
    probe = ProbeSpy(
        {broken.name: RuntimeError("unreadable"), good.name: _media()}
    )
    stamp = StampSpy()

    report = sweep_folder(
        tmp_path, mode=MODE, timezone=TZ, probe=probe, stamp=stamp, read_birth=_births({})
    )

    assert report.failed == 1
    assert report.stamped == 1
    assert report.results[0].path == broken
    assert report.results[0].error == "unreadable"


def test_non_video_files_and_records_are_ignored(tmp_path: Path):
    video = _write(tmp_path / "A.MP4")
    _write(tmp_path / "notes.txt")
    _write(tmp_path / ".vc-records" / "A.MP4.vc-meta.json")
    probe = ProbeSpy({video.name: _media()})
    stamp = StampSpy()

    report = sweep_folder(
        tmp_path, mode=MODE, timezone=TZ, probe=probe, stamp=stamp, read_birth=_births({})
    )

    assert report.ignored == 2
    assert report.scanned == 1
    assert [path.name for path in probe.calls] == [video.name]


def test_the_walk_is_sorted_and_recursive(tmp_path: Path):
    first = _write(tmp_path / "A.MP4")
    second = _write(tmp_path / "RIDE" / "B.MOV")
    third = _write(tmp_path / "RIDE" / "A.MP4")
    probe = ProbeSpy({path.name: _media() for path in (first, second, third)})
    stamp = StampSpy()

    report = sweep_folder(
        tmp_path, mode=MODE, timezone=TZ, probe=probe, stamp=stamp, read_birth=_births({})
    )

    paths = [result.path for result in report.results]
    assert paths == sorted(paths)
    assert {path.name for path in paths} == {"A.MP4", "B.MOV"}


def test_summarize_reports_stamped_and_failed_counts(tmp_path: Path):
    video = _write(tmp_path / "A.MP4")
    probe = ProbeSpy({video.name: _media()})

    report = sweep_folder(
        tmp_path, mode=MODE, timezone=TZ, probe=probe, stamp=StampSpy(), read_birth=_births({})
    )

    assert summarize(report) == "1 stamped, 0 failed"


@pytest.mark.skipif(sys.platform != "darwin", reason="birth times are macOS-only")
def test_real_stamp_round_trip_keeps_the_modification_time(tmp_path: Path):
    video = _write(tmp_path / "clip.mp4")
    target = datetime(2021, 5, 4, 12, 30, 45)
    before = video.stat()

    assert stamp_creation_time(video, target)

    assert read_creation_time(video) == target
    after = video.stat()
    assert after.st_mtime_ns == before.st_mtime_ns


# -- unsupported platforms (cross-platform: this is how Linux CI sees them) ----


def test_supported_matches_the_platform():
    assert supported() is (sys.platform == "darwin")


def test_birth_times_are_unavailable_off_macos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    video = _write(tmp_path / "A.MP4")
    monkeypatch.setattr(sys, "platform", "linux")

    assert supported() is False
    assert read_creation_time(video) is None
    assert stamp_creation_time(video, datetime(2021, 5, 4, 12, 30, 45)) is False


def test_sweep_reports_unsupported_platform_failures_without_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    video = _write(tmp_path / "A.MP4")
    probe = ProbeSpy({video.name: _media()})
    monkeypatch.setattr(sys, "platform", "linux")

    report = sweep_folder(tmp_path, mode=MODE, timezone=TZ, probe=probe)  # real seams

    assert report.stamped == 0
    assert report.failed == 1
    result = report.results[0]
    assert result.status is StampStatus.FAILED
    assert result.error == "the creation date was not set"
