"""Two-pass libx265 encoding: command construction, progress, and execution (issue #4).

The recipe matches the plan.md settings table: scale to 720-pixel height, average
video bitrate, AAC audio, QuickTime-compatible ``hvc1`` tagging, preserved source
metadata, while chapters and telemetry/data streams are dropped. FFmpeg runs on
the worker's local copy of the source; the archive is reached only through the
access-transport seam (docs/architecture.md).
"""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from shutil import disk_usage
from time import monotonic

from videocompressor.core.models import EncodeSettings

FFMPEG_BIN = "ffmpeg"

#: FFmpeg writes x265 two-pass stats next to the passlog prefix; current builds
#: produce ``<prefix>-0.log`` and ``<prefix>-0.log.cutree``.
PASS_LOG_ALLOWANCE_BYTES = 256 * 1024 * 1024

#: Extra breathing room required beyond source + output + pass logs.
SCRATCH_MARGIN_BYTES = 1024 * 1024 * 1024

_VIDEO_FILTER = "scale=-2:{height}"


class EncodeError(RuntimeError):
    """Raised when an FFmpeg pass exits unsuccessfully."""

    def __init__(
        self, message: str, *, pass_number: int | None = None, returncode: int | None = None
    ) -> None:
        super().__init__(message)
        self.pass_number = pass_number
        self.returncode = returncode


class EncodeStopped(RuntimeError):
    """Raised when the caller's stop event terminated the active pass."""


class ScratchSpaceError(RuntimeError):
    """Raised when scratch storage cannot hold source + output + pass logs + margin."""

    def __init__(self, message: str, *, required_bytes: int, available_bytes: int) -> None:
        super().__init__(message)
        self.required_bytes = required_bytes
        self.available_bytes = available_bytes


# ---------------------------------------------------------------------------
# Command construction
# ---------------------------------------------------------------------------


def build_pass1_command(
    settings: EncodeSettings,
    source: Path,
    *,
    passlog_prefix: Path,
    threads: int | None = None,
    ffmpeg_bin: str = FFMPEG_BIN,
) -> list[str]:
    """Build the first-pass analysis command; writes x265 stats to ``passlog_prefix``."""
    command = _base_command(
        settings, source, passlog_prefix, pass_number=1, threads=threads, ffmpeg_bin=ffmpeg_bin
    )
    command += ["-an", "-f", "null", "-"]
    return command


def build_pass2_command(
    settings: EncodeSettings,
    source: Path,
    output: Path,
    *,
    passlog_prefix: Path,
    threads: int | None = None,
    ffmpeg_bin: str = FFMPEG_BIN,
) -> list[str]:
    """Build the second-pass encode command producing ``output``."""
    command = _base_command(
        settings, source, passlog_prefix, pass_number=2, threads=threads, ffmpeg_bin=ffmpeg_bin
    )
    command += [
        "-map",
        "0:a?",
        "-c:a", settings.audio_codec,
        "-b:a", f"{settings.audio_bitrate_kbps}k",
        "-map_metadata",
        "0",
        "-map_chapters",
        "-1",
        "-tag:v",
        "hvc1",
        "-movflags",
        "+faststart",
        str(output),
    ]
    return command


def _base_command(
    settings: EncodeSettings,
    source: Path,
    passlog_prefix: Path,
    *,
    pass_number: int,
    threads: int | None,
    ffmpeg_bin: str,
) -> list[str]:
    command = [
        ffmpeg_bin,
        "-hide_banner",
        "-nostdin",
        "-y",
        "-progress",
        "pipe:1",
        "-nostats",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-vf",
        _VIDEO_FILTER.format(height=settings.output_height),
        "-pix_fmt",
        "yuv420p",
        "-c:v",
        "libx265",
        "-b:v",
        f"{settings.video_bitrate_kbps}k",
        "-preset",
        settings.ffmpeg_preset,
    ]
    if threads is not None:
        command += ["-threads", str(threads)]
    command += ["-pass", str(pass_number), "-passlogfile", str(passlog_prefix)]
    return command


# ---------------------------------------------------------------------------
# Progress parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EncodeProgress:
    """One parsed ``-progress pipe:1`` block for a single encoding pass."""

    pass_number: int
    frame: int | None = None
    fps: float | None = None
    speed: float | None = None
    out_time_s: float | None = None
    percent: float | None = None
    eta_seconds: float | None = None
    total_size_bytes: int | None = None


def compute_eta(
    duration_s: float | None, out_time_s: float | None, speed: float | None
) -> float | None:
    """Estimate remaining wall-clock seconds from media position and encoding speed."""
    if duration_s is None or out_time_s is None or speed is None or speed <= 0:
        return None
    remaining = duration_s - out_time_s
    if remaining <= 0:
        return 0.0
    return remaining / speed


class ProgressParser:
    """Accumulate ``-progress pipe:1`` key=value lines into per-block progress."""

    def __init__(self, *, pass_number: int, duration_s: float | None = None) -> None:
        self._pass_number = pass_number
        self._duration_s = duration_s
        self._block: dict[str, str] = {}

    def feed(self, line: str) -> EncodeProgress | None:
        """Feed one output line; returns progress when a block completes, else ``None``."""
        line = line.strip()
        if not line or "=" not in line:
            return None
        key, _, value = line.partition("=")
        self._block[key] = value
        if key != "progress":
            return None
        block = self._block
        self._block = {}
        return self._build(block)

    def _build(self, block: dict[str, str]) -> EncodeProgress:
        out_time_s = _parse_out_time(block)
        speed = _parse_float(block.get("speed"))
        percent = None
        if out_time_s is not None and self._duration_s:
            percent = max(0.0, min(100.0, 100.0 * out_time_s / self._duration_s))
        return EncodeProgress(
            pass_number=self._pass_number,
            frame=_parse_int(block.get("frame")),
            fps=_parse_float(block.get("fps")),
            speed=speed,
            out_time_s=out_time_s,
            percent=percent,
            eta_seconds=compute_eta(self._duration_s, out_time_s, speed),
            total_size_bytes=_parse_int(block.get("total_size")),
        )


def _parse_out_time(block: dict[str, str]) -> float | None:
    micros = block.get("out_time_us") or block.get("out_time_ms")
    if micros is not None and micros != "N/A":
        try:
            return int(micros) / 1_000_000
        except ValueError:
            pass
    stamp = block.get("out_time")
    if not stamp or stamp == "N/A":
        return None
    try:
        hours, minutes, seconds = stamp.split(":")
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except ValueError:
        return None


def _parse_float(value: str | None) -> float | None:
    if value is None or value == "N/A":
        return None
    try:
        return float(value.rstrip("x"))
    except ValueError:
        return None


def _parse_int(value: str | None) -> int | None:
    if value is None or value == "N/A":
        return None
    try:
        return int(value)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Size estimation and scratch-space gate
# ---------------------------------------------------------------------------


def estimate_output_bytes(
    duration_s: float,
    video_bitrate_kbps: int,
    audio_bitrate_kbps: int,
    *,
    audio_stream_count: int = 1,
) -> int:
    """Estimate the encoded output size from duration and configured bitrates.

    Container overhead is not modelled; the scratch gate adds margin for it.
    """
    if duration_s <= 0:
        return 0
    bits_per_second = (video_bitrate_kbps + audio_bitrate_kbps * audio_stream_count) * 1000
    return int(duration_s * bits_per_second / 8)


def required_scratch_bytes(
    *,
    source_bytes: int,
    estimated_output_bytes: int,
    pass_log_allowance_bytes: int = PASS_LOG_ALLOWANCE_BYTES,
    margin_bytes: int = SCRATCH_MARGIN_BYTES,
) -> int:
    """Total scratch bytes needed for one job: source + output + pass logs + margin."""
    return source_bytes + estimated_output_bytes + pass_log_allowance_bytes + margin_bytes


@dataclass(frozen=True)
class ScratchSpaceReport:
    """Scratch-space check outcome for one job."""

    path: Path
    required_bytes: int
    available_bytes: int

    @property
    def ok(self) -> bool:
        """Whether the scratch path has at least the required bytes free."""
        return self.available_bytes >= self.required_bytes


def check_scratch_space(
    scratch_dir: Path,
    *,
    source_bytes: int,
    estimated_output_bytes: int,
    pass_log_allowance_bytes: int = PASS_LOG_ALLOWANCE_BYTES,
    margin_bytes: int = SCRATCH_MARGIN_BYTES,
) -> ScratchSpaceReport:
    """Gate a job on free scratch space; raises ``ScratchSpaceError`` when short."""
    required = required_scratch_bytes(
        source_bytes=source_bytes,
        estimated_output_bytes=estimated_output_bytes,
        pass_log_allowance_bytes=pass_log_allowance_bytes,
        margin_bytes=margin_bytes,
    )
    available = disk_usage(scratch_dir).free
    report = ScratchSpaceReport(
        path=scratch_dir, required_bytes=required, available_bytes=available
    )
    if not report.ok:
        raise ScratchSpaceError(
            f"insufficient scratch space in {scratch_dir}: "
            f"need {required} bytes, {available} available",
            required_bytes=required,
            available_bytes=available,
        )
    return report


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EncodeResult:
    """Outcome of a successful two-pass encode."""

    output_path: Path
    output_bytes: int
    pass_seconds: tuple[float, float]
    final_progress: tuple[EncodeProgress | None, EncodeProgress | None]


def run_two_pass_encode(
    source: Path,
    output: Path,
    settings: EncodeSettings,
    *,
    duration_s: float | None,
    passlog_prefix: Path,
    threads: int | None = None,
    stop_event: threading.Event | None = None,
    on_progress: Callable[[EncodeProgress], None] | None = None,
    ffmpeg_bin: str = FFMPEG_BIN,
    stop_timeout_s: float = 10.0,
) -> EncodeResult:
    """Run both passes locally, streaming progress and honouring ``stop_event``.

    FFmpeg stderr for each pass is written to ``<passlog_prefix>.pass<N>.stderr.log``
    beside the pass log; issue #6 removes the scratch directory afterwards.
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    passlog_prefix.parent.mkdir(parents=True, exist_ok=True)

    commands = (
        build_pass1_command(
            settings, source, passlog_prefix=passlog_prefix, threads=threads, ffmpeg_bin=ffmpeg_bin
        ),
        build_pass2_command(
            settings,
            source,
            output,
            passlog_prefix=passlog_prefix,
            threads=threads,
            ffmpeg_bin=ffmpeg_bin,
        ),
    )

    elapsed: list[float] = []
    finals: list[EncodeProgress | None] = []
    for pass_number, command in enumerate(commands, start=1):
        if stop_event is not None and stop_event.is_set():
            raise EncodeStopped(f"encoding stopped before pass {pass_number}")
        parser = ProgressParser(pass_number=pass_number, duration_s=duration_s)
        seconds, final = _run_pass(
            command,
            pass_number=pass_number,
            parser=parser,
            on_progress=on_progress,
            stop_event=stop_event,
            stop_timeout_s=stop_timeout_s,
            stderr_path=_stderr_path(passlog_prefix, pass_number),
        )
        elapsed.append(seconds)
        finals.append(final)

    return EncodeResult(
        output_path=output,
        output_bytes=output.stat().st_size,
        pass_seconds=(elapsed[0], elapsed[1]),
        final_progress=(finals[0], finals[1]),
    )


def _run_pass(
    command: list[str],
    *,
    pass_number: int,
    parser: ProgressParser,
    on_progress: Callable[[EncodeProgress], None] | None,
    stop_event: threading.Event | None,
    stop_timeout_s: float,
    stderr_path: Path,
) -> tuple[float, EncodeProgress | None]:
    started = monotonic()
    with stderr_path.open("wb") as stderr_file:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=stderr_file,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert process.stdout is not None
        last: EncodeProgress | None = None
        try:
            for line in process.stdout:
                if stop_event is not None and stop_event.is_set():
                    _terminate(process, stop_timeout_s)
                    raise EncodeStopped(f"encoding stopped during pass {pass_number}")
                progress = parser.feed(line)
                if progress is not None:
                    last = progress
                    if on_progress is not None:
                        on_progress(progress)
            returncode = process.wait()
        finally:
            process.stdout.close()
    elapsed = monotonic() - started
    if returncode != 0:
        raise EncodeError(
            f"ffmpeg pass {pass_number} failed (exit {returncode}): {_stderr_tail(stderr_path)}",
            pass_number=pass_number,
            returncode=returncode,
        )
    return elapsed, last


def _terminate(process: subprocess.Popen[str], timeout_s: float) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _stderr_path(passlog_prefix: Path, pass_number: int) -> Path:
    return passlog_prefix.with_name(f"{passlog_prefix.name}.pass{pass_number}.stderr.log")


def _stderr_tail(path: Path, *, max_lines: int = 20) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "(stderr log unavailable)"
    return "\n".join(lines[-max_lines:]) or "(no stderr output)"
