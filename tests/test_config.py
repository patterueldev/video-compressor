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


def test_controller_defaults(tmp_path):
    config = load_config(tmp_path / "missing.toml")
    assert config.controller.host == "127.0.0.1"
    assert config.controller.port == 8765
    assert config.controller.token is None


def test_controller_round_trip(tmp_path):
    path = tmp_path / "config.toml"
    config = AppConfig()
    config.controller.host = "0.0.0.0"
    config.controller.port = 9000
    config.controller.token = "secret-token"
    save_config(config, path)

    text = path.read_text(encoding="utf-8")
    assert "[controller]" in text
    loaded = load_config(path)
    assert loaded.controller == config.controller


def test_controller_section_partial(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[controller]\nport = 9100\n", encoding="utf-8")

    config = load_config(path)
    assert config.controller.port == 9100
    assert config.controller.host == "127.0.0.1"
    assert config.controller.token is None
