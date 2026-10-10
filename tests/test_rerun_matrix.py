"""Rerun-matrix tooling tests (issue #13).

Loads ``scripts/pilot_common.py`` / ``matrix_common.py`` / ``rerun_matrix.py``
by path (they are operational scripts, not package modules) and covers the CLI
guards, staging/seed/lease helpers, and the sanitized report without ffmpeg.
The end-to-end synthetic matrix lives in ``test_rerun_matrix_slow.py``.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from videocompressor.core.fingerprint import fingerprint_file
from videocompressor.core.manifest import Manifest
from videocompressor.core.models import JobState

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def _load(name: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


pilot_common = _load("pilot_common")
matrix_common = _load("matrix_common")
rerun_matrix = _load("rerun_matrix")


class _SweepStub:
    """Controller stand-in: ``expire_lease`` must drive one reconciliation pass."""

    def __init__(self, manifest: Manifest) -> None:
        self.manifest = manifest
        self.calls = 0

    def sweep_once(self) -> None:
        self.calls += 1
        self.manifest.reconcile()


@pytest.fixture
def matrix_context(tmp_path: Path) -> Iterator[matrix_common.MatrixContext]:
    manifest = Manifest.open(tmp_path / "manifest.sqlite", create=True)
    context = matrix_common.MatrixContext(
        archive_dir=tmp_path / "archive",
        scratch_dir=tmp_path / "scratch",
        state_dir=tmp_path / "state",
        logs_dir=tmp_path / "logs",
        matrix_root=tmp_path,
        manifest=manifest,
        store=None,
        server=None,
        base_url="http://127.0.0.1:1",
        token="matrix-token",
        synthetic=True,
        timeout=5.0,
    )
    yield context
    manifest.close()


def test_scenario_metadata_matches_the_issue_wording() -> None:
    expected = ["rerun_skip", "changed_review", "interrupted_resume", "failed_recover"]
    assert [scenario_id for scenario_id, _ in rerun_matrix.SCENARIOS] == expected
    assert set(rerun_matrix.SCENARIO_FUNCS) == set(expected)


def test_dry_run_prints_scenarios_and_writes_nothing(tmp_path: Path, capsys) -> None:
    matrix = tmp_path / "matrix"
    code = rerun_matrix.main(["run", "--synthetic", "--dry-run", "--matrix-root", str(matrix)])
    assert code == 0
    output = capsys.readouterr().out
    for scenario_id, _ in rerun_matrix.SCENARIOS:
        assert scenario_id in output
    assert "nothing written" in output
    assert not matrix.exists()


def test_matrix_root_inside_the_archive_is_refused(tmp_path: Path, capsys) -> None:
    archive = tmp_path / "archive"
    archive.mkdir()
    code = rerun_matrix.main(
        [
            "run",
            "--archive-root", str(archive),
            "--clip", "ride.MP4",
            "--matrix-root", str(archive / "matrix"),
        ]
    )
    assert code == 2
    assert "must live outside the archive root" in capsys.readouterr().err
    assert not (archive / "matrix").exists()


def test_mode_guards_request_an_explicit_source(tmp_path: Path, capsys) -> None:
    matrix = tmp_path / "matrix"
    assert rerun_matrix.main(["run", "--matrix-root", str(matrix)]) == 2
    assert (
        rerun_matrix.main(
            ["run", "--synthetic", "--clip", "ride.MP4", "--matrix-root", str(matrix)]
        )
        == 2
    )
    assert "cannot be combined" in capsys.readouterr().err
    assert not matrix.exists()


def test_missing_archive_clip_fails_before_staging(tmp_path: Path, capsys) -> None:
    archive = tmp_path / "archive"
    archive.mkdir()
    code = rerun_matrix.main(
        [
            "run",
            "--archive-root", str(archive),
            "--clip", "missing.MP4",
            "--matrix-root", str(tmp_path / "matrix"),
        ]
    )
    assert code == 1
    assert "clip not found" in capsys.readouterr().err


def test_non_empty_matrix_root_is_refused(tmp_path: Path, capsys) -> None:
    matrix = tmp_path / "matrix"
    matrix.mkdir()
    (matrix / "keep.txt").write_text("operator data", encoding="utf-8")
    code = rerun_matrix.main(["run", "--synthetic", "--matrix-root", str(matrix)])
    assert code == 2
    assert "must be new or empty" in capsys.readouterr().err
    assert (matrix / "keep.txt").read_text(encoding="utf-8") == "operator data"


def test_stage_clip_copies_and_verifies(tmp_path: Path) -> None:
    source = tmp_path / "src.MP4"
    source.write_bytes(b"media-bytes" * 32)
    destination = tmp_path / "staged" / "dest.MP4"

    size, seconds = matrix_common.stage_clip(source, destination)

    assert size == source.stat().st_size
    assert destination.read_bytes() == source.read_bytes()
    assert seconds > 0


def test_seed_queued_records_the_clip_with_a_fallback_capture_time(
    matrix_context: matrix_common.MatrixContext,
) -> None:
    clip = matrix_context.matrix_root / "not-media.MP4"
    clip.write_bytes(b"not media bytes")
    relative = "rerun_skip/not-media.MP4"

    matrix_common.seed_queued(matrix_context, relative, clip)

    record = matrix_context.manifest.get_clip(matrix_common.ROOT_ID, relative)
    assert record is not None
    assert record.state is JobState.QUEUED
    assert record.source == fingerprint_file(clip)
    assert record.capture_time is not None and record.capture_time.when is not None
    assert record.capture_time.when.date().isoformat() == "2026-06-01"
    assert record.capture_time.when.hour == 12


def test_expire_lease_requeues_a_claimed_clip(
    matrix_context: matrix_common.MatrixContext,
) -> None:
    clip = matrix_context.matrix_root / "ride.MP4"
    clip.write_bytes(b"clip bytes")
    relative = "interrupted_resume/ride.MP4"
    matrix_common.seed_queued(matrix_context, relative, clip)
    assert matrix_context.manifest.claim_next("w1") is not None
    stub = _SweepStub(matrix_context.manifest)
    matrix_context.server = stub

    matrix_common.expire_lease(matrix_context, relative)

    record = matrix_context.manifest.get_clip(matrix_common.ROOT_ID, relative)
    assert record is not None and record.state is JobState.QUEUED
    assert "requeued after lease expiry" in (record.error or "")
    assert stub.calls == 1


def test_report_counts_checks_and_sanitizes_paths(
    tmp_path: Path, matrix_context: matrix_common.MatrixContext
) -> None:
    report = matrix_common.ScenarioReport("rerun_skip", "completed clips skip")
    report.check("passes", True)
    report.check("fails", False, f"detail under {tmp_path}/secret.MP4")
    document = matrix_common.build_report(
        synthetic=True,
        archive_root=None,
        matrix_root=tmp_path,
        staging=[
            {
                "relative_path": "rerun_skip/ride.MP4",
                "role": "subject",
                "source_bytes": 10,
                "staged_bytes": 10,
                "stage_seconds": 0.1,
                "stage_mb_per_s": 0.1,
            }
        ],
        reports=[report],
        manifest=matrix_context.manifest,
    )

    written = matrix_common.write_report(
        document, tmp_path / "report.json", {str(tmp_path): "<matrix-root>"}
    )

    text = written.read_text(encoding="utf-8")
    assert str(tmp_path) not in text
    assert "<matrix-root>" in text
    parsed = json.loads(text)
    assert parsed["totals"] == {
        "scenarios": 1,
        "failed_scenarios": 1,
        "checks": 2,
        "failures": 1,
    }
    assert parsed["clips"][0]["relative_path"] == "rerun_skip/ride.MP4"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg on PATH")
def test_failing_shim_exits_only_on_pass_two(tmp_path: Path) -> None:
    shim = matrix_common.write_failing_ffmpeg_shim(tmp_path / "shim")

    pass2 = subprocess.run(
        [str(shim), "-y", "-i", "in.MP4", "-pass", "2", "out.MP4"],
        capture_output=True,
        text=True,
    )
    delegate = subprocess.run([str(shim), "-version"], capture_output=True, text=True)

    assert pass2.returncode == 7
    assert "injected encode failure" in pass2.stderr
    assert delegate.returncode == 0
    assert "ffmpeg version" in delegate.stdout
