"""TOML codec for one template table (issue #87).

Extracted from ``core/config.py`` so the config module stays within its line
budget. Reading never fails a load: a stored pattern that is not a valid
:mod:`videocompressor.core.naming` pattern falls back to the default with a
warning, and templates that predate the ``name_pattern`` key load the default.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from videocompressor.core.models import OriginalHandling, TemplateSettings
from videocompressor.core.naming import validate_pattern

__all__ = ["template_from_toml", "template_to_toml"]

_log = logging.getLogger("videocompressor.core")


def template_to_toml(template: TemplateSettings) -> dict[str, object]:
    """The TOML table for one template, in the config file's shape."""
    return {
        "output_height": template.output_height,
        "video_bitrate_kbps": template.video_bitrate_kbps,
        "timezone": template.timezone,
        "audio_codec": template.audio_codec,
        "audio_bitrate_kbps": template.audio_bitrate_kbps,
        "ffmpeg_preset": template.ffmpeg_preset,
        "original_handling": str(template.original_handling),
        "name_pattern": template.name_pattern,
    }


def template_from_toml(name: str, data: Mapping[str, object]) -> TemplateSettings:
    """Parse one template table; missing keys take the defaults."""
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
        name_pattern=_template_name_pattern(name, data, defaults.name_pattern),
    )


def _template_name_pattern(name: str, data: Mapping[str, object], default: str) -> str:
    """Resolve the stored name pattern; invalid stored values read as the default."""
    raw = data.get("name_pattern", default)
    if not isinstance(raw, str):
        _log.warning(
            "template %r has a non-string name_pattern; using the default %r", name, default
        )
        return default
    problems = validate_pattern(raw)
    if problems:
        _log.warning(
            "template %r has an invalid name_pattern %r (%s); using the default %r",
            name,
            raw,
            "; ".join(problems),
            default,
        )
        return default
    return raw


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
