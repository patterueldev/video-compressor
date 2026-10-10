"""Media probing via ffprobe.

The frozen :func:`probe_media` interface comes from issue #1; the ffprobe
implementation lands here in issue #2. Every value comes from ffprobe's JSON
output; the filesystem is only checked for existence, never for modification
time.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from videocompressor.core.models import CaptureIssue, MediaInfo, MediaStream

#: ffprobe executable, resolved from ``PATH``.
FFPROBE_BIN = "ffprobe"

#: Give up on one probe after this many seconds (network shares can stall).
FFPROBE_TIMEOUT_S = 60.0

#: stderr markers that mean "could not read the file" rather than "not a media file".
_IO_ERROR_MARKERS = (
    "no such file or directory",
    "permission denied",
    "operation not permitted",
    "input/output error",
    "i/o error",
)


class ProbeError(RuntimeError):
    """Raised when a media file cannot be read or is unsupported.

    ``kind`` carries the review reason (issue #2) for the caller: ``unreadable``
    when the file could not be read, ``unsupported`` when ffprobe read it but it
    is not a media file we can process.
    """

    def __init__(self, message: str, *, kind: CaptureIssue = CaptureIssue.UNREADABLE) -> None:
        super().__init__(message)
        self.kind = kind


class FFprobeUnavailableError(RuntimeError):
    """Raised when the ``ffprobe`` executable cannot be found or started."""


def probe_media(path: Path) -> MediaInfo:
    """Probe ``path`` and return its streams, duration, bitrate, and tags.

    Implemented in issue #2. Never uses filesystem modification time.
    """
    if not path.is_file():
        raise ProbeError(f"not a readable file: {path}", kind=CaptureIssue.UNREADABLE)
    return parse_ffprobe_payload(_run_ffprobe(path), path)


def parse_ffprobe_payload(payload: Mapping[str, Any], path: Path) -> MediaInfo:
    """Turn one ffprobe JSON document into a :class:`MediaInfo`."""
    raw_streams = payload.get("streams") or ()
    if not isinstance(raw_streams, Sequence) or isinstance(raw_streams, str | bytes):
        raise ProbeError(f"{path}: ffprobe returned no stream list", kind=CaptureIssue.UNSUPPORTED)
    streams = tuple(_parse_stream(item, path) for item in raw_streams)
    if not streams:
        raise ProbeError(f"{path}: no media streams found", kind=CaptureIssue.UNSUPPORTED)

    raw_format = payload.get("format")
    format_data: Mapping[str, Any] = raw_format if isinstance(raw_format, Mapping) else {}
    return MediaInfo(
        path=path,
        format_name=_optional_str(format_data.get("format_name")),
        duration_s=_optional_float(format_data.get("duration")),
        size_bytes=_optional_int(format_data.get("size")),
        bit_rate=_optional_int(format_data.get("bit_rate")),
        format_tags=_string_tags(format_data.get("tags")),
        streams=streams,
    )


def _run_ffprobe(path: Path) -> Mapping[str, Any]:
    command = [
        FFPROBE_BIN,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=FFPROBE_TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError as exc:
        raise FFprobeUnavailableError(
            f"{FFPROBE_BIN} not found; install FFmpeg or add it to PATH"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ProbeError(
            f"{path}: ffprobe timed out after {FFPROBE_TIMEOUT_S:.0f}s",
            kind=CaptureIssue.UNREADABLE,
        ) from exc
    except OSError as exc:
        raise ProbeError(
            f"{path}: cannot run ffprobe: {exc}", kind=CaptureIssue.UNREADABLE
        ) from exc

    if completed.returncode != 0:
        raise _probe_error_from_stderr(completed.stderr, path)

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ProbeError(
            f"{path}: ffprobe returned invalid JSON", kind=CaptureIssue.UNSUPPORTED
        ) from exc
    if not isinstance(payload, Mapping):
        raise ProbeError(
            f"{path}: ffprobe returned unexpected JSON", kind=CaptureIssue.UNSUPPORTED
        )
    return payload


def _probe_error_from_stderr(stderr: str, path: Path) -> ProbeError:
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    detail = lines[-1] if lines else "ffprobe failed"
    kind = (
        CaptureIssue.UNREADABLE
        if any(marker in detail.lower() for marker in _IO_ERROR_MARKERS)
        else CaptureIssue.UNSUPPORTED
    )
    return ProbeError(f"{path}: {detail}", kind=kind)


def _parse_stream(item: Any, path: Path) -> MediaStream:
    if not isinstance(item, Mapping):
        raise ProbeError(
            f"{path}: malformed stream entry from ffprobe", kind=CaptureIssue.UNSUPPORTED
        )
    index = _optional_int(item.get("index"))
    return MediaStream(
        index=index if index is not None else 0,
        codec_type=str(item.get("codec_type") or "unknown"),
        codec_name=_optional_str(item.get("codec_name")),
        width=_optional_int(item.get("width")),
        height=_optional_int(item.get("height")),
        channels=_optional_int(item.get("channels")),
        codec_tag=_optional_str(item.get("codec_tag_string")),
        tags=_string_tags(item.get("tags")),
    )


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value) or None


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(str(value)))
        except (TypeError, ValueError):
            return None


def _optional_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _string_tags(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): str(tag) for key, tag in value.items() if tag is not None}
