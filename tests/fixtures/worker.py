"""Worker-domain fixtures (issue #6): config factory and pipeline fakes."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from videocompressor.core.ffmpeg import EncodeProgress, EncodeResult, EncodeStopped
from videocompressor.core.models import MediaInfo, MediaStream
from videocompressor.core.recover import RecoveryOutcome, RecoveryReport
from videocompressor.core.validate import ValidationResult
from videocompressor.worker.client import ClaimedJob
from videocompressor.worker.config import WorkerConfig

TOKEN = "test-token"

CLIP = "2024-06-Ride/GS010001.MP4"

DEFAULT_SETTINGS = {
    "output_height": 720,
    "video_bitrate_kbps": 800,
    "audio_bitrate_kbps": 96,
    "ffmpeg_preset": "medium",
}


def make_job(
    relative_path: str = CLIP,
    *,
    root_id: str = "archive",
    destination: dict[str, Any] | None = None,
    delete_original: bool | None = None,
    **settings: Any,
) -> ClaimedJob:
    """Build a claim payload for tests.

    ``destination`` is the raw convert-flow spec (issue #40); ``None`` keeps the
    legacy same-folder swap. ``delete_original`` is tri-state: ``None`` means the
    claim carried no flag.
    """
    return ClaimedJob(
        root_id=root_id,
        relative_path=relative_path,
        template_id="motorcycle",
        settings={**DEFAULT_SETTINGS, **settings},
        settings_hash="settings-hash",
        destination=destination,
        delete_original=delete_original,
    )


def raiser(exc: Exception):
    """Build a seam that always raises ``exc``."""

    def _raise(*args: Any, **kwargs: Any):
        raise exc

    return _raise


class FakeEncoder:
    """Stand-in for ``run_two_pass_encode`` with switchable behaviour.

    Modes: ``ok`` writes the payload and returns a result; ``error`` raises the
    given exception after writing; ``stopped`` writes a partial output and
    raises ``EncodeStopped``; ``block`` waits for the stop event and then raises.
    """

    def __init__(
        self,
        *,
        payload: bytes = b"encoded-bytes",
        mode: str = "ok",
        error: Exception | None = None,
    ) -> None:
        self.payload = payload
        self.mode = mode
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        source: Path,
        output: Path,
        settings: object,
        *,
        duration_s: float | None = None,
        passlog_prefix: Path | None = None,
        threads: int | None = None,
        stop_event: Any = None,
        on_progress: Callable[[EncodeProgress], None] | None = None,
        **kwargs: Any,
    ) -> EncodeResult:
        self.calls.append(
            {
                "source": source,
                "source_bytes": source.read_bytes(),
                "output": output,
                "settings": settings,
                "duration_s": duration_s,
                "passlog_prefix": passlog_prefix,
                "threads": threads,
                "stop_event": stop_event,
            }
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        if self.mode == "block":
            if on_progress is not None:
                on_progress(
                    EncodeProgress(
                        pass_number=2, percent=50.0, fps=30.0, speed=1.0, eta_seconds=2.0
                    )
                )
            assert stop_event is not None
            while not stop_event.is_set():
                time.sleep(0.01)
            raise EncodeStopped("stopped during fake encode")
        if self.mode == "stopped":
            output.write_bytes(self.payload + b"-partial")
            raise EncodeStopped("stopped during fake encode")
        output.write_bytes(self.payload)
        if on_progress is not None:
            on_progress(
                EncodeProgress(pass_number=2, percent=50.0, fps=30.0, speed=1.0, eta_seconds=2.0)
            )
        if self.error is not None:
            raise self.error
        return EncodeResult(
            output_path=output,
            output_bytes=len(self.payload),
            pass_seconds=(0.1, 0.2),
            final_progress=(None, None),
        )


@pytest.fixture
def fake_media_factory() -> Callable[..., MediaInfo]:
    """Build a plausible probed clip (video + optional audio)."""

    def make(
        *,
        duration_s: float | None = 4.0,
        width: int = 1920,
        height: int = 1080,
        has_audio: bool = True,
    ) -> MediaInfo:
        streams = [
            MediaStream(index=0, codec_type="video", codec_name="h264", width=width, height=height)
        ]
        if has_audio:
            streams.append(MediaStream(index=1, codec_type="audio", codec_name="aac"))
        return MediaInfo(
            path=Path("clip.mp4"),
            format_name="mov,mp4,m4a,3gp,3g2,mj2",
            duration_s=duration_s,
            streams=tuple(streams),
        )

    return make


@pytest.fixture
def fake_prober(fake_media_factory) -> Callable[..., Callable[[Path], MediaInfo]]:
    """Build a probe seam returning one media result for every path."""

    def make(media: MediaInfo | None = None) -> Callable[[Path], MediaInfo]:
        result = media if media is not None else fake_media_factory()
        return lambda path: result

    return make


@pytest.fixture
def fake_validator() -> Callable[..., Callable[..., ValidationResult]]:
    """Build a validate seam returning a fixed verdict."""

    def make(
        *, ok: bool = True, failures: tuple[str, ...] = ()
    ) -> Callable[..., ValidationResult]:
        def validate(output: Path, settings: object, *, source: object = None, **kwargs: Any):
            return ValidationResult(ok=ok, failures=failures, output=None)

        return validate

    return make


@pytest.fixture
def fake_recover() -> Callable[..., Callable[..., RecoveryReport]]:
    """Build a recovery seam returning a fixed outcome."""

    def make(
        outcome: RecoveryOutcome = RecoveryOutcome.NOTHING_TO_DO, detail: str | None = None
    ) -> Callable[..., RecoveryReport]:
        def recover(source: Path, **kwargs: Any) -> RecoveryReport:
            return RecoveryReport(source=source, outcome=outcome, detail=detail)

        return recover

    return make


@pytest.fixture
def worker_config_factory(tmp_path: Path) -> Callable[..., WorkerConfig]:
    """Build a WorkerConfig around ``tmp_path`` with fast test cadences."""

    def make(
        *,
        worker_id: str = "node-a",
        roots: dict[str, Path] | None = None,
        pilot_root: Path | None = None,
        scratch_dir: Path | None = None,
        **overrides: Any,
    ) -> WorkerConfig:
        archive = tmp_path / "archive"
        archive.mkdir(parents=True, exist_ok=True)
        scratch = scratch_dir if scratch_dir is not None else tmp_path / "scratch"
        scratch.mkdir(parents=True, exist_ok=True)
        if pilot_root is not None:
            pilot_root.mkdir(parents=True, exist_ok=True)
        fields: dict[str, Any] = {
            "worker_id": worker_id,
            "controller_url": "http://127.0.0.1:9",
            "token": TOKEN,
            "roots": roots if roots is not None else {"archive": archive},
            "scratch_dir": scratch,
            "pilot_root": pilot_root,
            "poll_seconds": 0.05,
            "heartbeat_seconds": 0.1,
        }
        fields.update(overrides)
        return WorkerConfig(**fields)

    return make
