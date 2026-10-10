"""probe_media: ffprobe JSON parsing, error mapping, and command wiring (issue #2)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from videocompressor.core.models import CaptureIssue
from videocompressor.core.probe import (
    FFPROBE_BIN,
    FFprobeUnavailableError,
    ProbeError,
    parse_ffprobe_payload,
    probe_media,
)

CLIP = Path("clip.MP4")


def test_parse_normal_ztagged_clip(ffprobe_fixture):
    media = parse_ffprobe_payload(ffprobe_fixture("normal_ztagged.json"), CLIP)

    assert media.path == CLIP
    assert media.format_name == "mov,mp4,m4a,3gp,3g2,mj2"
    assert media.duration_s == pytest.approx(1.0, abs=0.01)
    assert media.size_bytes and media.size_bytes > 0
    assert media.bit_rate and media.bit_rate > 0
    assert media.format_tags["creation_time"] == "2024-06-14T17:41:00.000000Z"

    assert [stream.index for stream in media.streams] == [0, 1]
    video, audio = media.streams
    assert (video.codec_type, video.codec_name) == ("video", "hevc")
    assert (video.width, video.height) == (320, 240)
    assert (audio.codec_type, audio.codec_name, audio.channels) == ("audio", "aac", 1)


def test_parse_no_audio_clip(ffprobe_fixture):
    media = parse_ffprobe_payload(ffprobe_fixture("no_audio.json"), CLIP)
    assert [stream.codec_type for stream in media.streams] == ["video"]


def test_parse_tiny_clip(ffprobe_fixture):
    media = parse_ffprobe_payload(ffprobe_fixture("tiny_clip.json"), CLIP)
    assert media.duration_s == pytest.approx(0.1, abs=0.01)
    assert len(media.streams) == 1


def test_parse_no_timestamp_clip(ffprobe_fixture):
    media = parse_ffprobe_payload(ffprobe_fixture("no_timestamp.json"), CLIP)
    assert "creation_time" not in media.format_tags
    assert all("creation_time" not in stream.tags for stream in media.streams)


def test_conflict_fixture_disagrees_between_format_and_video_stream(ffprobe_fixture):
    media = parse_ffprobe_payload(ffprobe_fixture("format_stream_conflict.json"), CLIP)
    assert media.format_tags["creation_time"] == "2024-06-14T17:41:00.000000Z"
    assert media.streams[0].tags["creation_time"] == "2024-06-14T09:00:00.000000Z"


def test_parse_tolerates_missing_numeric_fields():
    media = parse_ffprobe_payload(
        {
            "format": {"duration": "N/A", "size": "N/A", "bit_rate": "N/A"},
            "streams": [{"index": 0, "codec_type": "video", "width": "N/A"}],
        },
        CLIP,
    )
    assert media.duration_s is None
    assert media.size_bytes is None
    assert media.bit_rate is None
    assert media.streams[0].width is None


def test_parse_rejects_payload_without_streams():
    with pytest.raises(ProbeError) as excinfo:
        parse_ffprobe_payload({"format": {"format_name": "mp4"}}, CLIP)
    assert excinfo.value.kind is CaptureIssue.UNSUPPORTED


def test_probe_media_missing_file_is_unreadable(tmp_path):
    with pytest.raises(ProbeError) as excinfo:
        probe_media(tmp_path / "ghost.MP4")
    assert excinfo.value.kind is CaptureIssue.UNREADABLE


def test_probe_media_invokes_ffprobe_and_parses(tmp_path, monkeypatch, ffprobe_fixture):
    clip = tmp_path / "clip.MP4"
    clip.write_bytes(b"")
    calls: list[list[str]] = []
    payload = json.dumps(ffprobe_fixture("normal_ztagged.json"))

    def fake_run(command, **kwargs):
        calls.append(list(command))
        return subprocess.CompletedProcess(command, 0, stdout=payload, stderr="")

    monkeypatch.setattr("videocompressor.core.probe.subprocess.run", fake_run)

    media = probe_media(clip)

    assert calls == [
        [
            FFPROBE_BIN,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(clip),
        ]
    ]
    assert media.path == clip
    assert media.format_tags["creation_time"] == "2024-06-14T17:41:00.000000Z"


def test_probe_media_invalid_data_is_unsupported(tmp_path, monkeypatch):
    clip = tmp_path / "clip.MP4"
    clip.write_bytes(b"junk")
    _patch_ffprobe(
        monkeypatch, returncode=1, stderr="clip.MP4: Invalid data found when processing input\n"
    )

    with pytest.raises(ProbeError) as excinfo:
        probe_media(clip)
    assert excinfo.value.kind is CaptureIssue.UNSUPPORTED


def test_probe_media_permission_denied_is_unreadable(tmp_path, monkeypatch):
    clip = tmp_path / "clip.MP4"
    clip.write_bytes(b"")
    _patch_ffprobe(monkeypatch, returncode=1, stderr="clip.MP4: Permission denied\n")

    with pytest.raises(ProbeError) as excinfo:
        probe_media(clip)
    assert excinfo.value.kind is CaptureIssue.UNREADABLE


def test_probe_media_invalid_json_is_unsupported(tmp_path, monkeypatch):
    clip = tmp_path / "clip.MP4"
    clip.write_bytes(b"")
    _patch_ffprobe(monkeypatch, stdout="this is not json")

    with pytest.raises(ProbeError) as excinfo:
        probe_media(clip)
    assert excinfo.value.kind is CaptureIssue.UNSUPPORTED


def test_probe_media_timeout_is_unreadable(tmp_path, monkeypatch):
    clip = tmp_path / "clip.MP4"
    clip.write_bytes(b"")

    def fake_run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 60)

    monkeypatch.setattr("videocompressor.core.probe.subprocess.run", fake_run)

    with pytest.raises(ProbeError) as excinfo:
        probe_media(clip)
    assert excinfo.value.kind is CaptureIssue.UNREADABLE


def test_probe_media_without_ffprobe_binary(tmp_path, monkeypatch):
    clip = tmp_path / "clip.MP4"
    clip.write_bytes(b"")

    def fake_run(command, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", FFPROBE_BIN)

    monkeypatch.setattr("videocompressor.core.probe.subprocess.run", fake_run)

    with pytest.raises(FFprobeUnavailableError):
        probe_media(clip)


def _patch_ffprobe(monkeypatch, *, returncode=0, stdout="", stderr=""):
    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr("videocompressor.core.probe.subprocess.run", fake_run)


@pytest.mark.slow
def test_probe_media_against_real_ffprobe(tmp_path):
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg/ffprobe not installed")
    encoders = subprocess.run(
        ["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True, check=True
    )
    if "libx265" not in encoders.stdout:
        pytest.skip("ffmpeg is missing the libx265 encoder")

    clip = tmp_path / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x120:rate=10",
            "-t",
            "0.3",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx265",
            "-tag:v",
            "hvc1",
            "-metadata",
            "creation_time=2024-06-14T17:41:00.000000Z",
            "-an",
            str(clip),
        ],
        check=True,
    )

    media = probe_media(clip)

    assert media.format_tags["creation_time"] == "2024-06-14T17:41:00.000000Z"
    video = next(stream for stream in media.streams if stream.codec_type == "video")
    assert (video.codec_name, video.width, video.height) == ("hevc", 160, 120)
