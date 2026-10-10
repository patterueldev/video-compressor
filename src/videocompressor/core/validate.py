"""Output validation for encoded clips, used locally and after transfer back (issue #4).

Media facts come from :mod:`videocompressor.core.probe` (issue #2); this module adds
the encode-specific checks and the sampled decode pass.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from videocompressor.core.models import EncodeSettings, MediaInfo
from videocompressor.core.probe import ProbeError, probe_media

FFMPEG_BIN = "ffmpeg"

#: Output duration may differ from the source by a frame or two plus muxer rounding.
DURATION_TOLERANCE_S = 1.0
DURATION_TOLERANCE_FRACTION = 0.001

#: ``scale=-2:height`` rounds the proportional width to the nearest even pixel.
WIDTH_TOLERANCE_PX = 2

#: Sampled decode check: windows at the start, middle, and near the end.
DECODE_SAMPLE_SECONDS = 5.0
DECODE_SAMPLE_FRACTIONS = (0.0, 0.5, 0.9)


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


def facts_from_media_info(info: MediaInfo) -> MediaFacts:
    """Summarize a probed :class:`MediaInfo` into the facts validation compares."""
    video = next((stream for stream in info.streams if stream.codec_type == "video"), None)
    return MediaFacts(
        path=info.path,
        format_name=info.format_name,
        duration_s=info.duration_s,
        size_bytes=info.size_bytes,
        video_codec=video.codec_name if video is not None else None,
        video_codec_tag=video.codec_tag if video is not None else None,
        width=video.width if video is not None else None,
        height=video.height if video is not None else None,
        has_audio=any(stream.codec_type == "audio" for stream in info.streams),
        creation_time=info.format_tags.get("creation_time"),
        stream_codec_tags=tuple(
            stream.codec_tag for stream in info.streams if stream.codec_tag
        ),
    )


def probe_media_facts(path: Path) -> MediaFacts:
    """Probe ``path`` and summarize the facts validation needs."""
    return facts_from_media_info(probe_media(path))


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
    except ProbeError as exc:
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
