"""Pilot tooling tests (issue #12).

Loads ``scripts/pilot_common.py`` / ``pilot_run.py`` / ``bitrate_sweep.py`` by
path (they are operational scripts, not package modules) and covers their pure
logic without ffmpeg plus one slow sweep end-to-end test that needs ffmpeg.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import tomli_w

from videocompressor.core.fingerprint import fingerprint_file
from videocompressor.core.manifest import Manifest
from videocompressor.core.models import ClipRecord, Fingerprint, JobState

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
COMMITTED_SELECTION = ROOT / "docs" / "pilot" / "pilot-selection.toml"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


pilot_common = _load("pilot_common")
pilot_run = _load("pilot_run")
bitrate_sweep = _load("bitrate_sweep")


# -- helpers ------------------------------------------------------------------


def write_media(path: Path, payload: bytes = b"media-bytes") -> dict:
    """Write a fake clip and return its fingerprint fields."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    fingerprint = fingerprint_file(path)
    return {
        "size_bytes": fingerprint.size_bytes,
        "mtime_ns": fingerprint.mtime_ns,
        "digest": fingerprint.digest,
    }


def media_row(relative_path: str, media: dict, *, capture: str, bucket: str) -> dict:
    return {"relative_path": relative_path, "capture_time": capture, "bucket": bucket, **media}


def write_scan_report(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"clips": rows}), encoding="utf-8")


def clip_entry(
    relative_path: str,
    *,
    category: str = "daytime",
    bucket: str = "daytime",
    candidates: tuple[int, ...] = (800,),
    shots: list[dict] | None = None,
) -> dict:
    return {
        "relative_path": relative_path,
        "category": category,
        "bucket": bucket,
        "candidates": list(candidates),
        "shots": shots or [],
    }


def write_selection(path: Path, clips: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomli_w.dumps({"clips": clips}), encoding="utf-8")


def seed_manifest(path: Path, record: ClipRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with Manifest.open(path, create=True) as manifest:
        manifest.upsert_clip(record)


def seed_case(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A root with two fake clips, a scan report, and a matching selection."""
    root = tmp_path / "archive"
    rows, entries = [], []
    for relative_path, payload, capture, bucket, category in (
        ("folder/b.MP4", b"clip-b-bytes", "2025-06-02T18:00:00+08:00", "evening", "evening"),
        ("folder/a.MP4", b"clip-a-bytes", "2025-06-02T12:00:00+08:00", "daytime", "daytime"),
    ):
        rows.append(
            media_row(
                relative_path,
                write_media(root / relative_path, payload),
                capture=capture,
                bucket=bucket,
            )
        )
        entries.append(clip_entry(relative_path, category=category, bucket=bucket))
    report = tmp_path / "reports" / "scan.json"
    write_scan_report(report, rows)
    selection = tmp_path / "sel.toml"
    write_selection(selection, entries)
    return root, report, selection


def enqueue_args(
    root: Path, selection: Path, report: Path, manifest: Path, *extra: str
) -> list[str]:
    return [
        "enqueue", "--selection", str(selection), "--root", str(root),
        "--manifest", str(manifest), "--root-id", "camera", "--reports", str(report), *extra,
    ]


def collect_args(
    root: Path, pilot_root: Path, manifest: Path, out: Path, selection: Path | None = None
) -> list[str]:
    args = [
        "collect", "--manifest", str(manifest), "--root", str(root),
        "--pilot-root", str(pilot_root), "--out", str(out), "--no-probe",
    ]
    if selection is not None:
        args += ["--selection", str(selection)]
    return args


def make_clip(path: Path, *, seconds: int = 2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", f"testsrc2=size=320x240:rate=15:duration={seconds}",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
            "-c:v", "mpeg4", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path),
        ],
        check=True,
        capture_output=True,
    )


def test_load_selection_parses_shots_and_rejects_bad_files(tmp_path):
    path = tmp_path / "sel.toml"
    shot = {"t_s": 1.5, "crop": {"w": 100, "h": 50, "x": 10, "y": 20}, "note": "shop sign"}
    write_selection(path, [clip_entry("folder/clip.MP4", candidates=(600, 800), shots=[shot])])
    clips = pilot_common.load_selection(path)
    assert len(clips) == 1
    assert clips[0].candidates == (600, 800)
    assert (clips[0].shots[0].crop_w, clips[0].shots[0].crop_x) == (100, 10)
    assert clips[0].shots[0].note == "shop sign"

    with pytest.raises(pilot_common.SelectionError):
        pilot_common.load_selection(tmp_path / "missing.toml")

    bad = tmp_path / "bad.toml"

    def bad_entry(**overrides) -> dict:
        entry = clip_entry("a.MP4")
        entry.update(overrides)
        return entry

    bad_entries = (
        bad_entry(relative_path="../escape.MP4"),
        bad_entry(category="night"),
        bad_entry(candidates=[]),
        bad_entry(candidates=[0]),
    )
    for entry in bad_entries:
        write_selection(bad, [entry])
        with pytest.raises(pilot_common.SelectionError):
            pilot_common.load_selection(bad)


def test_committed_selection_is_valid():
    clips = pilot_common.load_selection(COMMITTED_SELECTION)
    assert {clip.category for clip in clips} == {"daytime", "evening", "dim", "motion-heavy"}
    for clip in clips:
        assert pilot_common.is_safe_relative_path(clip.relative_path)
        assert clip.bucket in pilot_common.BUCKETS
        assert clip.candidates and all(candidate > 0 for candidate in clip.candidates)


def test_helpers_sanitize_sizes_and_crops():
    assert pilot_common.slug("VIDEOS/MOTORCYCLE/A.MP4") == "VIDEOS__MOTORCYCLE__A"
    text = pilot_common.sanitize_text("x=/srv/archive/y", {"/srv/archive": "<archive-root>"})
    assert text == "x=<archive-root>/y"
    assert pilot_common.scaled_width_for_height(2560, 1440) == 1280
    assert pilot_common.scaled_width_for_height(320, 240) == 960

    shot = pilot_common.Shot(t_s=1.0, crop_w=480, crop_h=270, crop_x=800, crop_y=300)
    pilot_common.validate_crop(shot, 1280, 720)
    with pytest.raises(pilot_common.SelectionError):
        pilot_common.validate_crop(shot, 1000, 720)
    with pytest.raises(pilot_common.SelectionError):
        pilot_common.validate_crop(
            pilot_common.Shot(t_s=1.0, crop_w=1, crop_h=1, crop_x=0, crop_y=0), 1280, 720
        )


def test_enqueue_creates_ordered_queued_records(tmp_path):
    root, report, selection = seed_case(tmp_path)
    manifest_path = tmp_path / "pilot" / "manifest.sqlite"
    assert pilot_run.main(enqueue_args(root, selection, report, manifest_path)) == 0

    with Manifest.open(manifest_path, create=True) as manifest:
        record = manifest.get_clip("camera", "folder/b.MP4")
        assert record is not None
        assert record.state is JobState.QUEUED
        assert record.template_id == "motorcycle"
        assert record.capture_time is not None and record.capture_time.when is not None
        claimed = [manifest.claim_next("w"), manifest.claim_next("w")]
    assert [entry.relative_path for entry in claimed if entry is not None] == [
        "folder/b.MP4",
        "folder/a.MP4",
    ]


def test_enqueue_dry_run_writes_nothing(tmp_path):
    root, report, selection = seed_case(tmp_path)
    manifest_path = tmp_path / "manifest.sqlite"
    rc = pilot_run.main(enqueue_args(root, selection, report, manifest_path, "--dry-run"))
    assert rc == 0
    assert not manifest_path.exists()


def test_enqueue_refuses_changed_clip_unless_allowed(tmp_path):
    root, report, selection = seed_case(tmp_path)
    manifest_path = tmp_path / "manifest.sqlite"
    (root / "folder/b.MP4").write_bytes(b"mutated-after-the-scan")
    assert pilot_run.main(enqueue_args(root, selection, report, manifest_path)) == 1
    assert not manifest_path.exists()
    allowed = enqueue_args(root, selection, report, manifest_path, "--allow-changed")
    assert pilot_run.main(allowed) == 0


def test_enqueue_clobber_guard_and_requeue(tmp_path):
    root, report, selection = seed_case(tmp_path)
    manifest_path = tmp_path / "manifest.sqlite"
    args = enqueue_args(root, selection, report, manifest_path)
    assert pilot_run.main(args) == 0
    with Manifest.open(manifest_path, create=True) as manifest:
        manifest.update_state(
            "camera", "folder/b.MP4", JobState.COMPLETE, output=Fingerprint(1, 2, "done")
        )
    assert pilot_run.main(args) == 1
    assert pilot_run.main([*args, "--requeue"]) == 0
    with Manifest.open(manifest_path, create=False) as manifest:
        record = manifest.get_clip("camera", "folder/b.MP4")
    assert record is not None and record.state is JobState.QUEUED
    assert record.output is None and record.error is None


def test_enqueue_rejects_manifest_inside_root(tmp_path):
    root, report, selection = seed_case(tmp_path)
    manifest_inside = root / "manifest.sqlite"
    assert pilot_run.main(enqueue_args(root, selection, report, manifest_inside)) == 2
    assert not manifest_inside.exists()


def test_wait_times_out_then_finishes(tmp_path):
    manifest_path = tmp_path / "manifest.sqlite"
    seed_manifest(
        manifest_path,
        ClipRecord(
            root_id="camera", relative_path="folder/clip.MP4", source=Fingerprint(10, 20, "d")
        ),
    )
    assert (
        pilot_run.main(
            ["wait", "--manifest", str(manifest_path), "--timeout", "0.3", "--poll", "0.05"]
        )
        == 2
    )
    with Manifest.open(manifest_path, create=True) as manifest:
        manifest.update_state("camera", "folder/clip.MP4", JobState.REVIEW, error="pilot output")
    assert (
        pilot_run.main(
            ["wait", "--manifest", str(manifest_path), "--timeout", "2", "--poll", "0.05"]
        )
        == 0
    )


def test_collect_writes_sanitized_evidence(tmp_path):
    root = tmp_path / "archive"
    pilot_root = tmp_path / "pilot"
    relative_path = "folder/clip.MP4"
    media = write_media(root / relative_path, b"source-bytes")
    output = pilot_root / "folder" / "clip.MP4"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"pilot-bytes")

    manifest_path = tmp_path / "manifest.sqlite"
    seed_manifest(
        manifest_path,
        ClipRecord(
            root_id="camera",
            relative_path=relative_path,
            source=Fingerprint(**media),
            state=JobState.REVIEW,
            error=f"pilot output written to {output}; originals untouched",
        ),
    )
    selection = tmp_path / "sel.toml"
    write_selection(selection, [clip_entry(relative_path, category="evening", bucket="evening")])
    out = tmp_path / "pilot-run.json"
    rc = pilot_run.main(collect_args(root, pilot_root, manifest_path, out, selection))
    assert rc == 0
    text = out.read_text(encoding="utf-8")
    entry = json.loads(text)["clips"][0]
    assert entry["source_unchanged"] is True
    assert entry["output_exists"] is True and entry["output_bytes"] == 11
    assert entry["category"] == "evening"
    assert "<pilot-root>" in text
    assert str(tmp_path) not in text

    # a source that changed after seeding is flagged, not silently accepted
    (root / relative_path).write_bytes(b"replaced-bytes")
    assert pilot_run.main(collect_args(root, pilot_root, manifest_path, out)) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["clips"][0]["source_unchanged"] is False


def test_enqueue_single_clip_outside_selection(tmp_path):
    root, report, selection = seed_case(tmp_path)
    media = write_media(root / "folder/extra.MP4", b"extra-bytes")
    rows = json.loads(report.read_text(encoding="utf-8"))["clips"]
    rows.append(
        media_row("folder/extra.MP4", media, capture="2025-06-02T12:00:00+08:00", bucket="daytime")
    )
    write_scan_report(report, rows)
    manifest_path = tmp_path / "manifest.sqlite"
    args = enqueue_args(root, selection, report, manifest_path, "--clip", "folder/extra.MP4")
    assert pilot_run.main(args) == 0
    with Manifest.open(manifest_path, create=False) as manifest:
        record = manifest.get_clip("camera", "folder/extra.MP4")
    assert record is not None and record.state is JobState.QUEUED


def test_sweep_dry_run_prints_matrix_and_writes_nothing(tmp_path, capsys):
    selection = tmp_path / "sel.toml"
    write_selection(selection, [clip_entry("folder/clip.MP4", candidates=(600, 800))])
    root = tmp_path / "archive"
    root.mkdir()
    videos, review, report = tmp_path / "videos", tmp_path / "review", tmp_path / "sweep.json"
    rc = bitrate_sweep.main(
        [
            "run",
            "--selection", str(selection),
            "--root", str(root),
            "--videos-dir", str(videos),
            "--review-dir", str(review),
            "--report", str(report),
            "--dry-run",
        ]
    )
    assert rc == 0
    output = capsys.readouterr().out
    assert "600" in output and "800" in output and "dry run" in output
    assert not videos.exists() and not review.exists() and not report.exists()


def test_sweep_rejects_outputs_inside_root(tmp_path):
    selection = tmp_path / "sel.toml"
    write_selection(selection, [clip_entry("folder/clip.MP4")])
    root = tmp_path / "archive"
    root.mkdir()
    rc = bitrate_sweep.main(
        [
            "run",
            "--selection", str(selection),
            "--root", str(root),
            "--videos-dir", str(root / "sweep"),
            "--review-dir", str(tmp_path / "review"),
            "--report", str(tmp_path / "sweep.json"),
            "--dry-run",
        ]
    )
    assert rc == 2


@pytest.mark.slow
@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="needs ffmpeg and ffprobe on PATH",
)
def test_sweep_end_to_end_with_ffmpeg(tmp_path):
    root = tmp_path / "archive"
    relative_path = "folder/ride.MP4"
    make_clip(root / relative_path, seconds=2)
    selection = tmp_path / "sel.toml"
    write_selection(
        selection,
        [
            clip_entry(
                relative_path,
                candidates=(200, 600),
                shots=[
                    {
                        "t_s": 1,
                        "crop": {"w": 200, "h": 100, "x": 100, "y": 100},
                        "note": "sign",
                    }
                ],
            )
        ],
    )
    videos, review, report = tmp_path / "videos", tmp_path / "review", tmp_path / "sweep.json"
    rc = bitrate_sweep.main(
        [
            "run",
            "--selection", str(selection),
            "--root", str(root),
            "--videos-dir", str(videos),
            "--review-dir", str(review),
            "--report", str(report),
            "--scratch", str(tmp_path / "scratch"),
        ]
    )
    assert rc == 0
    clip_slug = pilot_common.slug(relative_path)
    assert (videos / clip_slug / "200kbps.mp4").is_file()
    assert (videos / clip_slug / "600kbps.mp4").is_file()
    stills = sorted(path.name for path in (review / clip_slug).glob("*.png"))
    assert stills == ["200kbps__t1__sign.png", "600kbps__t1__sign.png", "source__t1__sign.png"]

    text = report.read_text(encoding="utf-8")
    clip = json.loads(text)["clips"][0]
    assert all(candidate["validation"]["ok"] for candidate in clip["candidates"])
    assert clip["shots"][0]["stills"]["source"].endswith("source__t1__sign.png")
    assert str(tmp_path) not in text
