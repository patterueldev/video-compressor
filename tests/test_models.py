"""Frozen-model tests (issue #1 acceptance: template defaults, shared states)."""

from dataclasses import FrozenInstanceError
from datetime import time

import pytest

from videocompressor.core.models import (
    CaptureIssue,
    CaptureTimeMode,
    Classification,
    JobState,
    OriginalHandling,
    TemplateSettings,
)


def test_motorcycle_template_defaults_match_plan_md():
    settings = TemplateSettings()
    assert settings.output_height == 720
    assert settings.daytime_video_bitrate_kbps == 800
    assert settings.evening_video_bitrate_kbps == 800
    assert settings.evening_start == time(17, 30)
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


def test_capture_time_mode_defaults_to_local_wall_clock():
    assert CaptureTimeMode.LOCAL_WALL_CLOCK.value == "local_wall_clock"


def test_template_settings_are_frozen():
    settings = TemplateSettings()
    with pytest.raises(FrozenInstanceError):
        settings.output_height = 1080
