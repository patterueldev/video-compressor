"""Capture-time resolution: extraction, timezone modes, classification (issue #2)."""

from __future__ import annotations

from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from videocompressor.core.capture_time import bucket_for, resolve_capture_time
from videocompressor.core.models import (
    Bucket,
    CaptureIssue,
    CaptureTimeMode,
    MediaInfo,
    MediaStream,
)
from videocompressor.core.probe import parse_ffprobe_payload

MANILA = ZoneInfo("Asia/Manila")
UTC = ZoneInfo("UTC")

Z_TAGGED = "2024-06-14T17:41:00.000000Z"


def make_media(*, format_tags=None, streams=None) -> MediaInfo:
    return MediaInfo(
        path=Path("clip.MP4"),
        format_tags=format_tags or {},
        streams=tuple(streams or ()),
    )


def video_stream(index=0, *, tags=None) -> MediaStream:
    return MediaStream(index=index, codec_type="video", codec_name="hevc", tags=tags or {})


def audio_stream(index=1, *, tags=None) -> MediaStream:
    return MediaStream(index=index, codec_type="audio", codec_name="aac", tags=tags or {})


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (CaptureTimeMode.LOCAL_WALL_CLOCK, datetime(2024, 6, 14, 17, 41, tzinfo=MANILA)),
        (CaptureTimeMode.UTC, datetime(2024, 6, 15, 1, 41, tzinfo=MANILA)),
    ],
    ids=["local_wall_clock", "utc"],
)
def test_ztagged_timestamp_per_mode(mode, expected):
    media = make_media(
        format_tags={"creation_time": Z_TAGGED},
        streams=[video_stream(tags={"creation_time": Z_TAGGED})],
    )
    capture = resolve_capture_time(media, mode=mode, timezone="Asia/Manila")

    assert capture.when == expected
    assert capture.raw == Z_TAGGED
    assert capture.source == "format"
    assert capture.mode is mode
    assert capture.issue is None


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (CaptureTimeMode.LOCAL_WALL_CLOCK, datetime(2024, 6, 14, 9, 41, tzinfo=MANILA)),
        (CaptureTimeMode.UTC, datetime(2024, 6, 14, 17, 41, tzinfo=MANILA)),
    ],
    ids=["local_wall_clock", "utc"],
)
def test_plus_zero_offset_follows_the_same_rule_as_z(mode, expected):
    media = make_media(format_tags={"creation_time": "2024-06-14T09:41:00+00:00"})
    capture = resolve_capture_time(media, mode=mode, timezone="Asia/Manila")
    assert capture.when == expected
    assert capture.issue is None


@pytest.mark.parametrize("mode", list(CaptureTimeMode))
def test_naive_timestamp_is_wall_clock_in_configured_timezone(mode):
    media = make_media(format_tags={"creation_time": "2024-06-14T17:41:00"})
    capture = resolve_capture_time(media, mode=mode, timezone="Asia/Manila")
    assert capture.when == datetime(2024, 6, 14, 17, 41, tzinfo=MANILA)
    assert capture.issue is None


@pytest.mark.parametrize("mode", list(CaptureTimeMode))
def test_offset_timestamp_is_converted_to_configured_timezone(mode):
    media = make_media(format_tags={"creation_time": "2024-06-14T09:41:00-07:00"})
    capture = resolve_capture_time(media, mode=mode, timezone="Asia/Manila")
    # 09:41 in UTC-7 is 16:41 UTC, i.e. 00:41 the next day in Asia/Manila.
    assert capture.when == datetime(2024, 6, 15, 0, 41, tzinfo=MANILA)
    assert capture.issue is None


def test_stream_level_timestamp_used_when_format_lacks_one():
    media = make_media(streams=[video_stream(tags={"creation_time": Z_TAGGED}), audio_stream()])
    capture = resolve_capture_time(
        media, mode=CaptureTimeMode.LOCAL_WALL_CLOCK, timezone="Asia/Manila"
    )
    assert capture.source == "stream:0"
    assert capture.when == datetime(2024, 6, 14, 17, 41, tzinfo=MANILA)
    assert capture.issue is None


def test_format_level_wins_when_values_agree():
    media = make_media(
        format_tags={"creation_time": "2024-06-14T17:41:00Z"},
        streams=[video_stream(tags={"creation_time": Z_TAGGED})],
    )
    capture = resolve_capture_time(
        media, mode=CaptureTimeMode.LOCAL_WALL_CLOCK, timezone="Asia/Manila"
    )
    assert capture.source == "format"
    assert capture.raw == "2024-06-14T17:41:00Z"
    assert capture.issue is None


def test_format_and_stream_conflict_is_a_review_outcome():
    media = make_media(
        format_tags={"creation_time": Z_TAGGED},
        streams=[video_stream(tags={"creation_time": "2024-06-14T09:00:00.000000Z"})],
    )
    capture = resolve_capture_time(
        media, mode=CaptureTimeMode.LOCAL_WALL_CLOCK, timezone="Asia/Manila"
    )
    assert capture.when is None
    assert capture.issue is CaptureIssue.CONFLICTING_TIMESTAMPS
    assert capture.raw == Z_TAGGED
    assert capture.source == "format"


def test_conflicting_stream_timestamps_are_a_review_outcome():
    media = make_media(
        streams=[
            video_stream(tags={"creation_time": Z_TAGGED}),
            audio_stream(tags={"creation_time": "2024-06-14T18:41:00.000000Z"}),
        ]
    )
    capture = resolve_capture_time(
        media, mode=CaptureTimeMode.LOCAL_WALL_CLOCK, timezone="Asia/Manila"
    )
    assert capture.when is None
    assert capture.issue is CaptureIssue.CONFLICTING_TIMESTAMPS


def test_agreeing_streams_fall_back_to_the_first():
    media = make_media(
        streams=[
            video_stream(tags={"creation_time": "2024-06-14T17:41:00Z"}),
            audio_stream(tags={"creation_time": Z_TAGGED}),
        ]
    )
    capture = resolve_capture_time(
        media, mode=CaptureTimeMode.LOCAL_WALL_CLOCK, timezone="Asia/Manila"
    )
    assert capture.source == "stream:0"
    assert capture.issue is None


def test_missing_timestamp_is_a_review_outcome():
    capture = resolve_capture_time(
        make_media(), mode=CaptureTimeMode.LOCAL_WALL_CLOCK, timezone="Asia/Manila"
    )
    assert capture.when is None
    assert capture.issue is CaptureIssue.MISSING_TIMESTAMP
    assert capture.raw is None
    assert capture.source is None


def test_blank_timestamp_tag_counts_as_missing():
    media = make_media(format_tags={"creation_time": "   "})
    capture = resolve_capture_time(
        media, mode=CaptureTimeMode.LOCAL_WALL_CLOCK, timezone="Asia/Manila"
    )
    assert capture.issue is CaptureIssue.MISSING_TIMESTAMP


def test_unparseable_timestamp_reports_missing_with_raw_kept():
    media = make_media(format_tags={"creation_time": "not-a-timestamp"})
    capture = resolve_capture_time(
        media, mode=CaptureTimeMode.LOCAL_WALL_CLOCK, timezone="Asia/Manila"
    )
    assert capture.when is None
    assert capture.issue is CaptureIssue.MISSING_TIMESTAMP
    assert capture.raw == "not-a-timestamp"


def test_unparseable_format_falls_back_to_parseable_stream():
    media = make_media(
        format_tags={"creation_time": "not-a-timestamp"},
        streams=[video_stream(tags={"creation_time": Z_TAGGED})],
    )
    capture = resolve_capture_time(
        media, mode=CaptureTimeMode.LOCAL_WALL_CLOCK, timezone="Asia/Manila"
    )
    assert capture.issue is None
    assert capture.source == "stream:0"


def test_unknown_timezone_is_rejected():
    with pytest.raises(ValueError, match="unknown timezone"):
        resolve_capture_time(
            make_media(format_tags={"creation_time": Z_TAGGED}),
            mode=CaptureTimeMode.LOCAL_WALL_CLOCK,
            timezone="Mars/Olympus",
        )


@pytest.mark.parametrize(
    ("when", "expected"),
    [
        (datetime(2024, 6, 14, 0, 0, 0, tzinfo=MANILA), Bucket.DAYTIME),
        (datetime(2024, 6, 14, 17, 29, 59, tzinfo=MANILA), Bucket.DAYTIME),
        (datetime(2024, 6, 14, 17, 29, 59, 999999, tzinfo=MANILA), Bucket.DAYTIME),
        (datetime(2024, 6, 14, 17, 30, 0, tzinfo=MANILA), Bucket.EVENING),
        (datetime(2024, 6, 14, 23, 59, 59, 999999, tzinfo=MANILA), Bucket.EVENING),
    ],
    ids=["midnight", "17:29:59", "17:29:59.999999", "17:30:00", "23:59:59.999999"],
)
def test_evening_boundary_starts_at_17_30(when, expected):
    assert bucket_for(when, evening_start=time(17, 30), timezone="Asia/Manila") is expected


def test_bucket_converts_instants_into_configured_timezone():
    assert (
        bucket_for(
            datetime(2024, 6, 14, 9, 29, 59, tzinfo=UTC),
            evening_start=time(17, 30),
            timezone="Asia/Manila",
        )
        is Bucket.DAYTIME
    )
    assert (
        bucket_for(
            datetime(2024, 6, 14, 9, 30, 0, tzinfo=UTC),
            evening_start=time(17, 30),
            timezone="Asia/Manila",
        )
        is Bucket.EVENING
    )


def test_naive_bucket_time_is_read_as_configured_wall_clock():
    assert (
        bucket_for(
            datetime(2024, 6, 14, 17, 30), evening_start=time(17, 30), timezone="Asia/Manila"
        )
        is Bucket.EVENING
    )


@pytest.mark.parametrize(
    ("mode", "raw", "expected"),
    [
        (CaptureTimeMode.LOCAL_WALL_CLOCK, "2024-06-14T17:29:59Z", Bucket.DAYTIME),
        (CaptureTimeMode.LOCAL_WALL_CLOCK, "2024-06-14T17:30:00Z", Bucket.EVENING),
        (CaptureTimeMode.UTC, "2024-06-14T09:29:59Z", Bucket.DAYTIME),
        (CaptureTimeMode.UTC, "2024-06-14T09:30:00Z", Bucket.EVENING),
        # 17:30 UTC is 01:30 the next day in Asia/Manila: the mode changes the bucket.
        (CaptureTimeMode.UTC, "2024-06-14T17:30:00Z", Bucket.DAYTIME),
    ],
    ids=["local-before", "local-at", "utc-before", "utc-at", "utc-next-day"],
)
def test_classification_after_resolution(mode, raw, expected):
    media = make_media(format_tags={"creation_time": raw})
    capture = resolve_capture_time(media, mode=mode, timezone="Asia/Manila")
    assert capture.when is not None
    assert bucket_for(capture.when, evening_start=time(17, 30), timezone="Asia/Manila") is expected


def test_normal_fixture_resolves_as_wall_clock_evening(ffprobe_fixture):
    media = parse_ffprobe_payload(ffprobe_fixture("normal_ztagged.json"), Path("clip.MP4"))
    capture = resolve_capture_time(
        media, mode=CaptureTimeMode.LOCAL_WALL_CLOCK, timezone="Asia/Manila"
    )
    assert capture.issue is None
    assert capture.when == datetime(2024, 6, 14, 17, 41, tzinfo=MANILA)
    assert (
        bucket_for(capture.when, evening_start=time(17, 30), timezone="Asia/Manila")
        is Bucket.EVENING
    )


def test_normal_fixture_in_utc_mode_is_next_day_daytime(ffprobe_fixture):
    media = parse_ffprobe_payload(ffprobe_fixture("normal_ztagged.json"), Path("clip.MP4"))
    capture = resolve_capture_time(media, mode=CaptureTimeMode.UTC, timezone="Asia/Manila")
    assert capture.when == datetime(2024, 6, 15, 1, 41, tzinfo=MANILA)
    assert (
        bucket_for(capture.when, evening_start=time(17, 30), timezone="Asia/Manila")
        is Bucket.DAYTIME
    )


def test_conflict_fixture_is_a_review_outcome(ffprobe_fixture):
    media = parse_ffprobe_payload(ffprobe_fixture("format_stream_conflict.json"), Path("clip.MP4"))
    capture = resolve_capture_time(
        media, mode=CaptureTimeMode.LOCAL_WALL_CLOCK, timezone="Asia/Manila"
    )
    assert capture.when is None
    assert capture.issue is CaptureIssue.CONFLICTING_TIMESTAMPS


def test_no_timestamp_fixture_is_a_review_outcome(ffprobe_fixture):
    media = parse_ffprobe_payload(ffprobe_fixture("no_timestamp.json"), Path("clip.MP4"))
    capture = resolve_capture_time(
        media, mode=CaptureTimeMode.LOCAL_WALL_CLOCK, timezone="Asia/Manila"
    )
    assert capture.when is None
    assert capture.issue is CaptureIssue.MISSING_TIMESTAMP
