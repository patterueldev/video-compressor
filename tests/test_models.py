"""Frozen-model tests (issue #1 acceptance: template defaults, shared states)."""

from dataclasses import FrozenInstanceError

import pytest

from videocompressor.core.models import (
    Bucket,
    CaptureIssue,
    CaptureTimeMode,
    Classification,
    Destination,
    DestinationKind,
    EncodeSettings,
    JobState,
    OriginalHandling,
    TemplateSettings,
    destination_from_dict,
    destination_to_dict,
    is_safe_relative_path,
)


def test_motorcycle_template_defaults_match_plan_md():
    settings = TemplateSettings()
    assert settings.output_height == 720
    assert settings.video_bitrate_kbps == 800
    assert settings.timezone == "Asia/Manila"
    assert settings.audio_codec == "aac"
    assert settings.audio_bitrate_kbps == 96
    assert settings.ffmpeg_preset == "medium"
    assert settings.original_handling is OriginalHandling.REPLACE_AFTER_VALIDATED_TRANSFER


def test_job_state_members():
    assert {state.value for state in JobState} == {
        "queued",
        "claimed",
        "copied",
        "encoding",
        "validated",
        "transferring",
        "replacing",
        "complete",
        "failed",
        "review",
    }


def test_classification_members():
    assert {item.value for item in Classification} == {"new", "complete", "changed", "review"}


def test_capture_issue_members():
    assert {item.value for item in CaptureIssue} == {
        "missing_timestamp",
        "conflicting_timestamps",
        "unreadable",
        "unsupported",
    }


def test_bucket_members():
    assert {item.value for item in Bucket} == {"daytime", "evening"}


def test_capture_time_mode_defaults_to_local_wall_clock():
    assert CaptureTimeMode.LOCAL_WALL_CLOCK.value == "local_wall_clock"


def test_template_settings_are_frozen():
    settings = TemplateSettings()
    with pytest.raises(FrozenInstanceError):
        settings.output_height = 1080


def test_encode_settings_defaults_match_motorcycle_template():
    assert EncodeSettings() == EncodeSettings(
        output_height=720,
        video_bitrate_kbps=800,
        audio_bitrate_kbps=96,
        audio_codec="aac",
        ffmpeg_preset="medium",
    )


def test_encode_settings_reject_invalid_values():
    with pytest.raises(ValueError, match="even"):
        EncodeSettings(output_height=721)
    with pytest.raises(ValueError, match="even"):
        EncodeSettings(output_height=0)
    with pytest.raises(ValueError, match="video_bitrate"):
        EncodeSettings(video_bitrate_kbps=0)
    with pytest.raises(ValueError, match="audio_bitrate"):
        EncodeSettings(audio_bitrate_kbps=-1)


def test_destination_same_folder_takes_no_root_or_path():
    destination = Destination(DestinationKind.SAME_FOLDER)
    assert destination_to_dict(destination) == {"kind": "same_folder"}
    with pytest.raises(ValueError, match="same-folder"):
        Destination(DestinationKind.SAME_FOLDER, root_id="archive")
    with pytest.raises(ValueError, match="same-folder"):
        Destination(DestinationKind.SAME_FOLDER, relative_path="2026-10")


def test_destination_output_root_requires_a_safe_relative_path():
    destination = Destination(
        DestinationKind.OUTPUT_ROOT, root_id="compressed", relative_path="2026-10"
    )
    assert destination_to_dict(destination) == {
        "kind": "output_root",
        "root_id": "compressed",
        "relative_path": "2026-10",
    }
    assert destination_from_dict(destination_to_dict(destination)) == destination

    mirror = Destination(DestinationKind.OUTPUT_ROOT, "compressed", "")
    assert destination_to_dict(mirror) == {"kind": "output_root", "root_id": "compressed"}
    assert destination_from_dict(destination_to_dict(mirror)) == mirror

    with pytest.raises(ValueError, match="root_id"):
        Destination(DestinationKind.OUTPUT_ROOT)
    with pytest.raises(ValueError, match="unsafe"):
        Destination(DestinationKind.OUTPUT_ROOT, "compressed", "../escape")


def test_destination_dict_validation():
    assert destination_from_dict(None) is None
    assert destination_to_dict(None) is None
    with pytest.raises(ValueError, match="object"):
        destination_from_dict("same_folder")
    with pytest.raises(ValueError, match="kind"):
        destination_from_dict({"kind": "elsewhere"})
    with pytest.raises(ValueError, match="root_id"):
        destination_from_dict({"kind": "output_root", "root_id": 7})
    with pytest.raises(ValueError, match="relative_path"):
        destination_from_dict({"kind": "output_root", "root_id": "compressed", "relative_path": 7})


def test_is_safe_relative_path():
    assert is_safe_relative_path("a/b.MP4")
    assert not is_safe_relative_path("")
    assert is_safe_relative_path("", allow_empty=True)
    assert not is_safe_relative_path("../a.MP4")
    assert not is_safe_relative_path("/abs/a.MP4")
    assert not is_safe_relative_path("a\\b.MP4")
