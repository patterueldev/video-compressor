"""Per-user config tests (issue #1 acceptance: config round-trip)."""

import logging
from dataclasses import replace
from pathlib import Path

from videocompressor.core.config import (
    DEFAULT_TEMPLATE_NAME,
    AppConfig,
    CaptureTimeSettings,
    default_config_path,
    duplicate_template,
    load_config,
    rename_template,
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


def test_round_trip_toml(tmp_path):
    original = AppConfig(
        capture_time=CaptureTimeSettings(mode=CaptureTimeMode.UTC),
        templates={
            DEFAULT_TEMPLATE_NAME: replace(TemplateSettings(), video_bitrate_kbps=1200)
        },
    )
    path = save_config(original, tmp_path / "config.toml")

    text = path.read_text(encoding="utf-8")
    assert "[capture_time]" in text
    assert "[templates.motorcycle]" in text
    assert "video_bitrate_kbps = 1200" in text
    assert "daytime_video_bitrate_kbps" not in text
    assert "evening_video_bitrate_kbps" not in text
    assert "evening_start" not in text

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
    assert config.controller.orphan_grace_seconds == 300


def test_controller_round_trip(tmp_path):
    path = tmp_path / "config.toml"
    config = AppConfig()
    config.controller.host = "0.0.0.0"
    config.controller.port = 9000
    config.controller.token = "secret-token"
    config.controller.orphan_grace_seconds = 120
    save_config(config, path)

    text = path.read_text(encoding="utf-8")
    assert "[controller]" in text
    assert "orphan_grace_seconds = 120" in text
    loaded = load_config(path)
    assert loaded.controller == config.controller


def test_controller_section_partial(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[controller]\nport = 9100\norphan_grace_seconds = 45\n", encoding="utf-8")

    config = load_config(path)
    assert config.controller.port == 9100
    assert config.controller.host == "127.0.0.1"
    assert config.controller.token is None
    assert config.controller.orphan_grace_seconds == 45


def test_controller_orphan_grace_invalid_values_keep_the_default(tmp_path):
    path = tmp_path / "config.toml"
    for value in ("0", "-60", '"soon"'):
        path.write_text(f"[controller]\norphan_grace_seconds = {value}\n", encoding="utf-8")
        assert load_config(path).controller.orphan_grace_seconds == 300


def _write_legacy_template(path: Path, **bitrates: int) -> None:
    """Write a template table in the pre-#43 day/evening shape."""
    lines = ["[templates.motorcycle]", "output_height = 720"]
    lines += [f"{key} = {value}" for key, value in bitrates.items()]
    lines += [
        "evening_start = 17:30:00",
        'timezone = "Asia/Manila"',
        'audio_codec = "aac"',
        "audio_bitrate_kbps = 96",
        'ffmpeg_preset = "medium"',
        'original_handling = "replace_after_validated_transfer"',
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _core_warnings(caplog) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == "videocompressor.core"]


def test_legacy_day_evening_bitrates_migrate_to_the_single_key(tmp_path, caplog):
    path = tmp_path / "config.toml"
    _write_legacy_template(
        path, daytime_video_bitrate_kbps=800, evening_video_bitrate_kbps=800
    )

    with caplog.at_level(logging.WARNING, logger="videocompressor.core"):
        config = load_config(path)

    assert config.templates[DEFAULT_TEMPLATE_NAME] == TemplateSettings()
    assert config.templates[DEFAULT_TEMPLATE_NAME].video_bitrate_kbps == 800
    assert _core_warnings(caplog) == []


def test_differing_legacy_bitrates_use_the_larger_value_and_warn(tmp_path, caplog):
    path = tmp_path / "config.toml"
    _write_legacy_template(
        path, daytime_video_bitrate_kbps=1200, evening_video_bitrate_kbps=900
    )

    with caplog.at_level(logging.WARNING, logger="videocompressor.core"):
        config = load_config(path)

    assert config.templates[DEFAULT_TEMPLATE_NAME].video_bitrate_kbps == 1200
    warnings = _core_warnings(caplog)
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "motorcycle" in message and "1200" in message and "900" in message


def test_a_single_legacy_bitrate_is_used_as_is(tmp_path):
    path = tmp_path / "config.toml"
    _write_legacy_template(path, evening_video_bitrate_kbps=500)

    assert load_config(path).templates[DEFAULT_TEMPLATE_NAME].video_bitrate_kbps == 500


def test_legacy_template_without_bitrates_falls_back_to_the_default(tmp_path):
    path = tmp_path / "config.toml"
    _write_legacy_template(path)

    template = load_config(path).templates[DEFAULT_TEMPLATE_NAME]
    assert template.video_bitrate_kbps == 800


def test_the_new_key_wins_over_legacy_keys(tmp_path, caplog):
    path = tmp_path / "config.toml"
    _write_legacy_template(
        path,
        video_bitrate_kbps=900,
        daytime_video_bitrate_kbps=1200,
        evening_video_bitrate_kbps=900,
    )

    with caplog.at_level(logging.WARNING, logger="videocompressor.core"):
        config = load_config(path)

    assert config.templates[DEFAULT_TEMPLATE_NAME].video_bitrate_kbps == 900
    assert _core_warnings(caplog) == []


def test_saving_a_loaded_legacy_config_writes_only_the_new_shape(tmp_path):
    legacy = tmp_path / "legacy.toml"
    _write_legacy_template(
        legacy, daytime_video_bitrate_kbps=1200, evening_video_bitrate_kbps=900
    )
    config = load_config(legacy)

    saved = save_config(config, tmp_path / "config.toml")
    text = saved.read_text(encoding="utf-8")

    assert "video_bitrate_kbps = 1200" in text
    assert "daytime_video_bitrate_kbps" not in text
    assert "evening_video_bitrate_kbps" not in text
    assert "evening_start" not in text
    assert load_config(saved) == config


# -- output name patterns (issue #87) ----------------------------------------


def test_round_trip_persists_the_name_pattern(tmp_path):
    pattern = "{date}_{name}"
    original = AppConfig(
        templates={DEFAULT_TEMPLATE_NAME: replace(TemplateSettings(), name_pattern=pattern)}
    )

    path = save_config(original, tmp_path / "config.toml")

    assert f'name_pattern = "{pattern}"' in path.read_text(encoding="utf-8")
    assert load_config(path).templates[DEFAULT_TEMPLATE_NAME].name_pattern == pattern


def test_template_without_a_name_pattern_loads_the_default(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[templates.motorcycle]\noutput_height = 720\n", encoding="utf-8")

    template = load_config(path).templates[DEFAULT_TEMPLATE_NAME]
    assert template.name_pattern == "{name}"


def test_invalid_stored_name_pattern_falls_back_to_the_default_with_a_warning(tmp_path, caplog):
    path = tmp_path / "config.toml"
    path.write_text(
        '[templates.motorcycle]\nname_pattern = "{camera}_{name}"\n', encoding="utf-8"
    )

    with caplog.at_level(logging.WARNING, logger="videocompressor.core"):
        config = load_config(path)

    assert config.templates[DEFAULT_TEMPLATE_NAME].name_pattern == "{name}"
    warnings = _core_warnings(caplog)
    assert len(warnings) == 1
    assert "name_pattern" in warnings[0].getMessage()


def test_non_string_name_pattern_falls_back_to_the_default(tmp_path, caplog):
    path = tmp_path / "config.toml"
    path.write_text("[templates.motorcycle]\nname_pattern = 7\n", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="videocompressor.core"):
        config = load_config(path)

    assert config.templates[DEFAULT_TEMPLATE_NAME].name_pattern == "{name}"
    assert len(_core_warnings(caplog)) == 1


def test_duplicate_and_rename_preserve_the_name_pattern():
    config = AppConfig(
        templates={
            DEFAULT_TEMPLATE_NAME: replace(TemplateSettings(), name_pattern="{date}_{name}")
        }
    )

    duplicate_template(config, DEFAULT_TEMPLATE_NAME, "copy")
    assert config.templates["copy"].name_pattern == "{date}_{name}"

    rename_template(config, "copy", "renamed")
    assert config.templates["renamed"].name_pattern == "{date}_{name}"
