"""Output validation for encoded clips, used locally and after transfer back (issue #4).

Validation runs with a self-contained ffprobe reader (:func:`probe_media_facts`).
When issue #2's ``core.probe.probe_media`` lands, the reader can delegate to it
without changing this module's checks.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from videocompressor.core.models import EncodeSettings

FFPROBE_BIN = "ffprobe"
FFMPEG_BIN = "ffmpeg"

#: Output duration may differ from the source by a frame or two plus muxer rounding.
DURATION_TOLERANCE_S = 1.0
DURATION_TOLERANCE_FRACTION = 0.001

#: ``scale=-2:height`` rounds the proportional width to the nearest even pixel.
WIDTH_TOLERANCE_PX = 2

#: Sampled decode check: windows at the start, middle, and near the end.
DECODE_SAMPLE_SECONDS = 5.0
DECODE_SAMPLE_FRACTIONS = (0.0, 0.5, 0.9)


class MediaProbeError(RuntimeError):
    """Raised when ffprobe cannot read a file or its output cannot be parsed."""


@dataclass(frozen=True)
class MediaFacts:
    """The media facts ffprobe reports and validation compares."""

    path: Path
    format_name: str | None
    duration_s: float | None
    size_bytes: int | None
    video_codec: str | None
    video_codec_tag: str | None
    width: int | None
    height: int | None
    has_audio: bool
    creation_time: str | None
    stream_codec_tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class ValidationResult:
    """Outcome of validating one encoded output against its source and settings."""

    ok: bool
    failures: tuple[str, ...]
    output: MediaFacts | None = None


def probe_media_facts(path: Path, *, ffprobe_bin: str = FFPROBE_BIN) -> MediaFacts:
    """Read the container/stream facts validation needs from ``path``."""
    command = [
        ffprobe_bin,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    completed = _run_command(command)
    if completed.returncode != 0:
        detail = _one_line(completed.stderr) or f"ffprobe exit {completed.returncode}"
        raise MediaProbeError(f"cannot probe {path}: {detail}")
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise MediaProbeError(f"cannot parse ffprobe output for {path}: {exc}") from exc

    fmt = payload.get("format") or {}
    streams = payload.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    tags = fmt.get("tags") or {}
    return MediaFacts(
        path=path,
        format_name=fmt.get("format_name"),
        duration_s=_to_float(fmt.get("duration")),
        size_bytes=_to_int(fmt.get("size")),
        video_codec=video.get("codec_name"),
        video_codec_tag=video.get("codec_tag_string"),
        width=video.get("width"),
        height=video.get("height"),
        has_audio=has_audio,
        creation_time=tags.get("creation_time"),
        stream_codec_tags=tuple(
            tag for stream in streams if (tag := stream.get("codec_tag_string"))
        ),
    )


def validate_output(
    output: Path,
    settings: EncodeSettings,
    *,
    source: MediaFacts,
    probe: Callable[[Path], MediaFacts] | None = None,
    decode_check: bool = True,
    run: Callable[[Sequence[str]], subprocess.CompletedProcess[str]] | None = None,
    ffmpeg_bin: str = FFMPEG_BIN,
) -> ValidationResult:
    """Validate ``output`` against the source facts and the effective settings.

    Checks: readable container, H.265/HEVC video, exact output height, even and
    proportional width, duration close to the source, audio present if and only
    if the source had audio, and a sampled decode check.
    """
    if source.duration_s is None:
        raise ValueError("source duration is required to validate an output")

    prober = probe if probe is not None else probe_media_facts
    runner = run if run is not None else _run_command
    try:
        facts = prober(output)
    except MediaProbeError as exc:
        return ValidationResult(ok=False, failures=(f"unreadable: {exc}",), output=None)

    failures: list[str] = []

    if facts.video_codec != "hevc":
        failures.append(f"codec: expected hevc, found {facts.video_codec or 'none'}")

    if facts.height != settings.output_height:
        failures.append(f"height: expected {settings.output_height}, found {facts.height}")

    if facts.width is None:
        failures.append("width: output has no video width")
    else:
        if facts.width % 2:
            failures.append(f"width: odd width {facts.width} is not allowed")
        expected_width = expected_output_width(source.width, source.height, settings.output_height)
        if expected_width is not None and abs(facts.width - expected_width) > WIDTH_TOLERANCE_PX:
            failures.append(
                f"width: expected {expected_width} (proportional), found {facts.width}"
            )

    tolerance = duration_tolerance(source.duration_s)
    if facts.duration_s is None:
        failures.append("duration: output reports no duration")
    elif abs(facts.duration_s - source.duration_s) > tolerance:
        failures.append(
            f"duration: source {source.duration_s:.3f}s, output {facts.duration_s:.3f}s "
            f"(tolerance {tolerance:.3f}s)"
        )

    if source.has_audio and not facts.has_audio:
        failures.append("audio: source has audio, output has none")
    if facts.has_audio and not source.has_audio:
        failures.append("audio: output has audio, source has none")

    if decode_check:
        problem = sample_decode_check(
            output,
            duration_s=facts.duration_s if facts.duration_s is not None else source.duration_s,
            run=runner,
            ffmpeg_bin=ffmpeg_bin,
        )
        if problem is not None:
            failures.append(f"decode: {problem}")

    return ValidationResult(ok=not failures, failures=tuple(failures), output=facts)


def expected_output_width(
    source_width: int | None, source_height: int | None, output_height: int
) -> int | None:
    """Width ``scale=-2:output_height`` produces for the source, or ``None`` if unknown."""
    if not source_width or not source_height:
        return None
    width = round(source_width * output_height / source_height)
    return width - (width % 2)


def duration_tolerance(source_duration_s: float) -> float:
    """Allowed output/source duration difference for ``source_duration_s``."""
    return max(DURATION_TOLERANCE_S, DURATION_TOLERANCE_FRACTION * source_duration_s)


def sample_decode_check(
    path: Path,
    *,
    duration_s: float | None,
    run: Callable[[Sequence[str]], subprocess.CompletedProcess[str]] | None = None,
    ffmpeg_bin: str = FFMPEG_BIN,
) -> str | None:
    """Decode sampled windows; returns a failure description or ``None`` when clean."""
    runner = run if run is not None else _run_command
    if duration_s is not None and duration_s > 3 * DECODE_SAMPLE_SECONDS:
        span = duration_s - DECODE_SAMPLE_SECONDS
        starts = [fraction * span for fraction in DECODE_SAMPLE_FRACTIONS]
    else:
        starts = [0.0]
    for start in starts:
        command = [
            ffmpeg_bin,
            "-hide_banner",
            "-v",
            "error",
            "-nostdin",
            "-ss",
            f"{start:.3f}",
            "-i",
            str(path),
            "-t",
            str(DECODE_SAMPLE_SECONDS),
            "-f",
            "null",
            "-",
        ]
        completed = runner(command)
        if completed.returncode != 0 or completed.stderr.strip():
            detail = _one_line(completed.stderr) or f"exit {completed.returncode}"
            return f"decode failed at {start:.1f}s: {detail}"
    return None


def _run_command(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True)


def _one_line(text: str | None) -> str:
    if not text:
        return ""
    return next((line for line in text.splitlines() if line.strip()), "").strip()


def _to_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _to_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
