"""Per-user configuration stored as human-readable TOML.

Path (macOS): ``~/Library/Application Support/Video Compressor/config.toml``.

Reading uses the standard library (``tomllib``); writing uses ``tomli-w``. A missing
file loads as defaults, so the app always has a usable motorcycle template.

The GUI adds registered archive roots (issue #8) plus template create/duplicate/rename
helpers; every helper validates its input before the caller writes the file.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import tomli_w

from videocompressor.core.models import (
    ArchiveRoot,
    CaptureTimeMode,
    OriginalHandling,
    TemplateSettings,
)

DEFAULT_TEMPLATE_NAME = "motorcycle"

#: Registered archive-root ids double as manifest keys and job payload ids.
ROOT_ID_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,64}")

CONFIG_FILENAME = "config.toml"

MANIFEST_FILENAME = "manifest.sqlite"

DEFAULT_CONTROLLER_HOST = "127.0.0.1"
DEFAULT_CONTROLLER_PORT = 8765


def default_config_path() -> Path:
    """Return the per-user config path for this app."""
    return Path.home() / "Library" / "Application Support" / "Video Compressor" / CONFIG_FILENAME


def default_manifest_path() -> Path:
    """Return the per-user manifest database path for this app."""
    return default_config_path().parent / MANIFEST_FILENAME


@dataclass
class CaptureTimeSettings:
    """Capture-time interpretation settings (consumed by issue #2)."""

    mode: CaptureTimeMode = CaptureTimeMode.LOCAL_WALL_CLOCK


@dataclass
class ControllerSettings:
    """Local controller bind address and bearer token (issue #7)."""

    host: str = DEFAULT_CONTROLLER_HOST
    port: int = DEFAULT_CONTROLLER_PORT
    token: str | None = None


@dataclass
class AppConfig:
    """The per-user configuration document."""

    capture_time: CaptureTimeSettings = field(default_factory=CaptureTimeSettings)
    templates: dict[str, TemplateSettings] = field(
        default_factory=lambda: {DEFAULT_TEMPLATE_NAME: TemplateSettings()}
    )
    roots: list[ArchiveRoot] = field(default_factory=list)
    controller: ControllerSettings = field(default_factory=ControllerSettings)


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


def validate_root_id(value: str) -> str:
    """Return the cleaned root id, or raise ``ValueError`` when it is invalid."""
    root_id = value.strip()
    if not ROOT_ID_PATTERN.fullmatch(root_id):
        raise ValueError("root id must be 1-64 letters, digits, dot, underscore or dash")
    return root_id


def validate_template_name(value: str) -> str:
    """Return the template name, or raise ``ValueError`` when it is invalid."""
    if not value or value != value.strip():
        raise ValueError("template name must be non-empty without surrounding spaces")
    return value


def validate_template(settings: TemplateSettings) -> list[str]:
    """Return human-readable problems with ``settings`` (empty when valid)."""
    problems: list[str] = []
    if settings.output_height < 2 or settings.output_height % 2:
        problems.append("output height must be a positive even number")
    if settings.daytime_video_bitrate_kbps <= 0:
        problems.append("daytime video bitrate must be positive")
    if settings.evening_video_bitrate_kbps <= 0:
        problems.append("evening video bitrate must be positive")
    if settings.audio_bitrate_kbps <= 0:
        problems.append("audio bitrate must be positive")
    if not settings.audio_codec.strip():
        problems.append("audio codec must not be empty")
    if not settings.ffmpeg_preset.strip():
        problems.append("ffmpeg preset must not be empty")
    try:
        ZoneInfo(settings.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        problems.append(f"unknown timezone {settings.timezone!r}")
    return problems


def add_template(
    config: AppConfig, name: str, settings: TemplateSettings | None = None
) -> TemplateSettings:
    """Add a named template (motorcycle defaults unless ``settings`` is given)."""
    clean = validate_template_name(name)
    if clean in config.templates:
        raise ValueError(f"template {clean!r} already exists")
    config.templates[clean] = settings if settings is not None else TemplateSettings()
    return config.templates[clean]


def duplicate_template(config: AppConfig, source_name: str, new_name: str) -> TemplateSettings:
    """Copy an existing template under ``new_name``."""
    source = config.templates.get(source_name)
    if source is None:
        raise ValueError(f"unknown template {source_name!r}")
    return add_template(config, new_name, replace(source))


def rename_template(config: AppConfig, old_name: str, new_name: str) -> TemplateSettings:
    """Rename a template, preserving its settings and ordering."""
    clean = validate_template_name(new_name)
    if old_name not in config.templates:
        raise ValueError(f"unknown template {old_name!r}")
    if clean != old_name and clean in config.templates:
        raise ValueError(f"template {clean!r} already exists")
    config.templates = {
        (clean if key == old_name else key): value
        for key, value in config.templates.items()
    }
    return config.templates[clean]


def register_root(config: AppConfig, *, root_id: str, name: str, path: Path) -> ArchiveRoot:
    """Register an archive root, rejecting duplicate ids, paths, and non-directories."""
    clean_id = validate_root_id(root_id)
    if any(root.id == clean_id for root in config.roots):
        raise ValueError(f"root id {clean_id!r} is already registered")
    resolved = path.expanduser()
    if not resolved.is_absolute():
        raise ValueError("root path must be absolute")
    if any(root.path == resolved for root in config.roots):
        raise ValueError(f"root path {resolved} is already registered")
    if not resolved.is_dir():
        raise ValueError(f"root path {resolved} is not a directory")
    root = ArchiveRoot(id=clean_id, name=name.strip() or clean_id, path=resolved)
    config.roots.append(root)
    return root


def root_for_folder(roots: Sequence[ArchiveRoot], folder: Path) -> ArchiveRoot | None:
    """Return the most specific registered root containing ``folder``."""
    target = folder.expanduser().resolve()
    best: ArchiveRoot | None = None
    best_parts = -1
    for root in roots:
        root_path = root.path.expanduser().resolve()
        if target != root_path and root_path not in target.parents:
            continue
        if len(root_path.parts) > best_parts:
            best, best_parts = root, len(root_path.parts)
    return best


def _config_to_toml(config: AppConfig) -> dict[str, object]:
    controller: dict[str, object] = {
        "host": config.controller.host,
        "port": config.controller.port,
    }
    if config.controller.token is not None:
        controller["token"] = config.controller.token
    document: dict[str, object] = {
        "capture_time": {"mode": str(config.capture_time.mode)},
        "templates": {
            name: _template_to_toml(template) for name, template in config.templates.items()
        },
    }
    if config.roots:
        document["roots"] = [
            {"id": root.id, "name": root.name, "path": str(root.path)} for root in config.roots
        ]
    document["controller"] = controller
    return document


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

    roots = data.get("roots")
    if isinstance(roots, list):
        config.roots = [
            ArchiveRoot(
                id=str(entry["id"]),
                name=str(entry["name"]),
                path=Path(str(entry["path"])),
            )
            for entry in roots
            if isinstance(entry, Mapping) and {"id", "name", "path"} <= set(entry)
        ]

    controller = data.get("controller")
    if isinstance(controller, Mapping):
        config.controller.host = str(controller.get("host", config.controller.host))
        config.controller.port = int(controller.get("port", config.controller.port))
        token = controller.get("token")
        config.controller.token = str(token) if token is not None else None

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
