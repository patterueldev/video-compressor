"""Media probing via ffprobe.

Frozen in issue #1; the ffprobe implementation lands in issue #2.
"""

from __future__ import annotations

from pathlib import Path

from videocompressor.core.models import MediaInfo


class ProbeError(RuntimeError):
    """Raised when a media file cannot be read or is unsupported."""


def probe_media(path: Path) -> MediaInfo:
    """Probe ``path`` and return its streams, duration, bitrate, and tags.

    Implemented in issue #2. Never uses filesystem modification time.
    """
    raise NotImplementedError("probe_media is implemented in issue #2")
