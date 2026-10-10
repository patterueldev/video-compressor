"""Per-user config tests (issue #1 acceptance: config round-trip)."""

from dataclasses import replace
from datetime import time
from pathlib import Path

from videocompressor.core.config import (
    DEFAULT_TEMPLATE_NAME,
    AppConfig,
    CaptureTimeSettings,
    default_config_path,
    load_config,
    save_config,
)
from videocompressor.core.models import CaptureTimeMode, TemplateSettings


def test_default_config_path_is_per_user_macos_location():
    path = default_config_path()
    expected = Path.home() / "Library" / "Application Support" / "Video Compressor" / "config.toml"
    assert path == expected


def test_missing_file_loads_defaults(tmp_path):
    config = load_config(tmp_path / "missing.toml")
    assert config.capture_time.mode is CaptureTimeMode.LOCAL_WALL_CLOCK
    template = config.templates[DEFAULT_TEMPLATE_NAME]
    assert template == TemplateSettings()
    assert template.timezone == "Asia/Manila"
    assert template.evening_start == time(17, 30)


def test_round_trip_toml(tmp_path):
    original = AppConfig(
        capture_time=CaptureTimeSettings(mode=CaptureTimeMode.UTC),
        templates={
            DEFAULT_TEMPLATE_NAME: replace(TemplateSettings(), evening_video_bitrate_kbps=1500)
        },
    )
    path = save_config(original, tmp_path / "config.toml")

    text = path.read_text(encoding="utf-8")
    assert "[capture_time]" in text
    assert "[templates.motorcycle]" in text
    assert "17:30" in text

    assert load_config(path) == original


def test_partial_file_fills_defaults(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[capture_time]\nmode = "utc"\n', encoding="utf-8")

    config = load_config(path)
    assert config.capture_time.mode is CaptureTimeMode.UTC
    assert config.templates[DEFAULT_TEMPLATE_NAME] == TemplateSettings()
