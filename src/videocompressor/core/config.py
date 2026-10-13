"""Per-user configuration stored as human-readable TOML.

Path (macOS): ``~/Library/Application Support/Video Compressor/config.toml``.

Reading uses the standard library (``tomllib``); writing uses ``tomli-w``. A missing
file loads as defaults, so the app always has a usable motorcycle template.

Legacy templates carried ``daytime_video_bitrate_kbps`` and
``evening_video_bitrate_kbps`` side by side. They migrate to the single
``video_bitrate_kbps``: the present value, or the larger one when both are present
(a differing pair is logged as a warning). Reading never writes the file; the next
save emits the new shape only.

The GUI adds registered archive roots (issue #8) plus template create/duplicate/rename
helpers; every helper validates its input before the caller writes the file. The Nodes
tab (issue #54) adds ``[[nodes]]`` entries via ``core.nodes``, and the ``[local_worker]``
section (issue #52) is the app-managed local worker's setting.
"""

from __future__ import annotations

import logging
import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import tomli_w

from videocompressor.core.models import (
    ArchiveRoot,
    CaptureTimeMode,
    OriginalHandling,
    TemplateSettings,
)
from videocompressor.core.nodes import NodeEntry, nodes_from_toml, nodes_to_toml

_log = logging.getLogger("videocompressor.core")

DEFAULT_TEMPLATE_NAME = "motorcycle"

#: Registered archive-root ids double as manifest keys and job payload ids.
ROOT_ID_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,64}")

#: App-managed local worker defaults (issue #52): node id and FFmpeg thread cap.
LOCAL_WORKER_ID = "local"
DEFAULT_LOCAL_THREAD_CAP = 2

CONFIG_FILENAME = "config.toml"

MANIFEST_FILENAME = "manifest.sqlite"

DEFAULT_CONTROLLER_HOST = "127.0.0.1"
DEFAULT_CONTROLLER_PORT = 8765
DEFAULT_ORPHAN_GRACE_SECONDS = 300


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
    """Local controller bind address, bearer token, and orphan grace (issues #7, #75)."""

    host: str = DEFAULT_CONTROLLER_HOST
    port: int = DEFAULT_CONTROLLER_PORT
    token: str | None = None
    orphan_grace_seconds: int = DEFAULT_ORPHAN_GRACE_SECONDS


@dataclass
class LocalWorkerSettings:
    """App-managed local worker setting (issue #52): runs by default."""

    enabled: bool = True
    worker_id: str = LOCAL_WORKER_ID
    keep_running: bool = False
    scratch_dir: Path | None = None
    thread_cap: int = DEFAULT_LOCAL_THREAD_CAP


@dataclass
class AppConfig:
    """The per-user configuration document."""

    capture_time: CaptureTimeSettings = field(default_factory=CaptureTimeSettings)
    templates: dict[str, TemplateSettings] = field(
        default_factory=lambda: {DEFAULT_TEMPLATE_NAME: TemplateSettings()}
    )
    roots: list[ArchiveRoot] = field(default_factory=list)
    nodes: list[NodeEntry] = field(default_factory=list)
    controller: ControllerSettings = field(default_factory=ControllerSettings)
    local_worker: LocalWorkerSettings = field(default_factory=LocalWorkerSettings)


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
    if settings.video_bitrate_kbps <= 0:
        problems.append("video bitrate must be positive")
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


def validate_local_worker(
    settings: LocalWorkerSettings, roots: Sequence[ArchiveRoot] = ()
) -> list[str]:
    """Return human-readable problems with ``settings`` (empty when valid)."""
    problems: list[str] = []
    if not ROOT_ID_PATTERN.fullmatch(settings.worker_id):
        problems.append("local worker id must be 1-64 letters, digits, dot, underscore or dash")
    if settings.thread_cap < 1:
        problems.append("thread cap must be a positive integer")
    scratch = settings.scratch_dir
    if scratch is not None:
        resolved = scratch.expanduser()
        if not resolved.is_absolute():
            problems.append("scratch dir must be an absolute path")
        elif root_for_folder(roots, resolved) is not None:
            problems.append("scratch dir must not be inside a registered root")
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
        "orphan_grace_seconds": config.controller.orphan_grace_seconds,
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
    if config.nodes:
        document["nodes"] = nodes_to_toml(config.nodes)
    document["controller"] = controller
    local_worker: dict[str, object] = {
        "enabled": config.local_worker.enabled,
        "id": config.local_worker.worker_id,
        "keep_running": config.local_worker.keep_running,
        "thread_cap": config.local_worker.thread_cap,
    }
    if config.local_worker.scratch_dir is not None:
        local_worker["scratch_dir"] = str(config.local_worker.scratch_dir)
    document["local_worker"] = local_worker
    return document


def _template_to_toml(template: TemplateSettings) -> dict[str, object]:
    return {
        "output_height": template.output_height,
        "video_bitrate_kbps": template.video_bitrate_kbps,
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
            str(name): _template_from_toml(str(name), entry)
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

    config.nodes = nodes_from_toml(data.get("nodes"))

    controller = data.get("controller")
    if isinstance(controller, Mapping):
        config.controller.host = str(controller.get("host", config.controller.host))
        config.controller.port = int(controller.get("port", config.controller.port))
        token = controller.get("token")
        config.controller.token = str(token) if token is not None else None
        grace = controller.get("orphan_grace_seconds")
        if isinstance(grace, int) and not isinstance(grace, bool) and grace > 0:
            config.controller.orphan_grace_seconds = grace

    local_worker = data.get("local_worker")
    if isinstance(local_worker, Mapping):
        settings = config.local_worker
        enabled = local_worker.get("enabled")
        if isinstance(enabled, bool):
            settings.enabled = enabled
        worker_id = local_worker.get("id")
        if isinstance(worker_id, str) and worker_id:
            settings.worker_id = worker_id
        keep_running = local_worker.get("keep_running")
        if isinstance(keep_running, bool):
            settings.keep_running = keep_running
        thread_cap = local_worker.get("thread_cap")
        if isinstance(thread_cap, int) and not isinstance(thread_cap, bool):
            settings.thread_cap = thread_cap
        scratch_dir = local_worker.get("scratch_dir")
        if isinstance(scratch_dir, str) and scratch_dir:
            settings.scratch_dir = Path(scratch_dir).expanduser()

    return config


def _template_from_toml(name: str, data: Mapping[str, object]) -> TemplateSettings:
    defaults = TemplateSettings()
    return TemplateSettings(
        output_height=int(data.get("output_height", defaults.output_height)),
        video_bitrate_kbps=_template_video_bitrate(name, data, defaults.video_bitrate_kbps),
        timezone=str(data.get("timezone", defaults.timezone)),
        audio_codec=str(data.get("audio_codec", defaults.audio_codec)),
        audio_bitrate_kbps=int(data.get("audio_bitrate_kbps", defaults.audio_bitrate_kbps)),
        ffmpeg_preset=str(data.get("ffmpeg_preset", defaults.ffmpeg_preset)),
        original_handling=OriginalHandling(
            str(data.get("original_handling", defaults.original_handling))
        ),
    )


def _template_video_bitrate(name: str, data: Mapping[str, object], default: int) -> int:
    """Resolve the single template bitrate, migrating legacy day/evening values.

    The new key wins. Otherwise the present legacy value is used, or the larger
    of the two when both are present; a differing pair logs one warning, because
    the old file configured two knobs and only one survives. ``evening_start``
    has no replacement and is ignored.
    """
    if "video_bitrate_kbps" in data:
        return int(data["video_bitrate_kbps"])
    legacy = [
        int(data[key])
        for key in ("daytime_video_bitrate_kbps", "evening_video_bitrate_kbps")
        if key in data
    ]
    if not legacy:
        return default
    merged = max(legacy)
    if len(legacy) == 2 and legacy[0] != legacy[1]:
        _log.warning(
            "template %r has differing day/evening bitrates (%s); using the larger "
            "value %s kbps as the single video bitrate",
            name,
            " / ".join(f"{value} kbps" for value in legacy),
            merged,
        )
    return merged
