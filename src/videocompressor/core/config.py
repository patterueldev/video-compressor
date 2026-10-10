"""Per-user configuration stored as human-readable TOML.

Path (macOS): ``~/Library/Application Support/Video Compressor/config.toml``.

Reading uses the standard library (``tomllib``); writing uses ``tomli-w``. A missing
file loads as defaults, so the app always has a usable motorcycle template.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path

import tomli_w

from videocompressor.core.models import CaptureTimeMode, OriginalHandling, TemplateSettings

DEFAULT_TEMPLATE_NAME = "motorcycle"

CONFIG_FILENAME = "config.toml"


def default_config_path() -> Path:
    """Return the per-user config path for this app."""
    return Path.home() / "Library" / "Application Support" / "Video Compressor" / CONFIG_FILENAME


@dataclass
class CaptureTimeSettings:
    """Capture-time interpretation settings (consumed by issue #2)."""

    mode: CaptureTimeMode = CaptureTimeMode.LOCAL_WALL_CLOCK


@dataclass
class AppConfig:
    """The per-user configuration document."""

    capture_time: CaptureTimeSettings = field(default_factory=CaptureTimeSettings)
    templates: dict[str, TemplateSettings] = field(
        default_factory=lambda: {DEFAULT_TEMPLATE_NAME: TemplateSettings()}
    )


def load_config(path: Path | None = None) -> AppConfig:
    """Load configuration from ``path`` (default: per-user path); missing file means defaults."""
    config_path = path if path is not None else default_config_path()
    if not config_path.is_file():
        return AppConfig()
    with config_path.open("rb") as handle:
        data = tomllib.load(handle)
    return _config_from_toml(data)


def save_config(config: AppConfig, path: Path | None = None) -> Path:
    """Write ``config`` as TOML to ``path`` (default: per-user path); returns the path."""
    config_path = path if path is not None else default_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with config_path.open("wb") as handle:
        tomli_w.dump(_config_to_toml(config), handle)
    return config_path


def _config_to_toml(config: AppConfig) -> dict[str, object]:
    return {
        "capture_time": {"mode": str(config.capture_time.mode)},
        "templates": {
            name: _template_to_toml(template) for name, template in config.templates.items()
        },
    }


def _template_to_toml(template: TemplateSettings) -> dict[str, object]:
    return {
        "output_height": template.output_height,
        "daytime_video_bitrate_kbps": template.daytime_video_bitrate_kbps,
        "evening_video_bitrate_kbps": template.evening_video_bitrate_kbps,
        "evening_start": template.evening_start,
        "timezone": template.timezone,
        "audio_codec": template.audio_codec,
        "audio_bitrate_kbps": template.audio_bitrate_kbps,
        "ffmpeg_preset": template.ffmpeg_preset,
        "original_handling": str(template.original_handling),
    }


def _config_from_toml(data: Mapping[str, object]) -> AppConfig:
    config = AppConfig()

    capture = data.get("capture_time")
    if isinstance(capture, Mapping):
        mode = str(capture.get("mode", CaptureTimeMode.LOCAL_WALL_CLOCK))
        config.capture_time.mode = CaptureTimeMode(mode)

    templates = data.get("templates")
    if isinstance(templates, Mapping):
        parsed = {
            str(name): _template_from_toml(entry)
            for name, entry in templates.items()
            if isinstance(entry, Mapping)
        }
        if parsed:
            config.templates = parsed

    return config


def _template_from_toml(data: Mapping[str, object]) -> TemplateSettings:
    defaults = TemplateSettings()
    return TemplateSettings(
        output_height=int(data.get("output_height", defaults.output_height)),
        daytime_video_bitrate_kbps=int(
            data.get("daytime_video_bitrate_kbps", defaults.daytime_video_bitrate_kbps)
        ),
        evening_video_bitrate_kbps=int(
            data.get("evening_video_bitrate_kbps", defaults.evening_video_bitrate_kbps)
        ),
        evening_start=_parse_time(data.get("evening_start", defaults.evening_start)),
        timezone=str(data.get("timezone", defaults.timezone)),
        audio_codec=str(data.get("audio_codec", defaults.audio_codec)),
        audio_bitrate_kbps=int(data.get("audio_bitrate_kbps", defaults.audio_bitrate_kbps)),
        ffmpeg_preset=str(data.get("ffmpeg_preset", defaults.ffmpeg_preset)),
        original_handling=OriginalHandling(
            str(data.get("original_handling", defaults.original_handling))
        ),
    )


def _parse_time(value: object) -> time:
    if isinstance(value, time):
        return value
    return time.fromisoformat(str(value))
