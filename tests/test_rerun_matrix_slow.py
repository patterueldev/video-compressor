"""Slow end-to-end rerun matrix (issue #13): real ffmpeg, controller, and workers.

Runs the four synthetic scenarios through ``scripts/rerun_matrix.py`` and checks
the sanitized evidence report: reruns skip completed clips after a template
change, changed sources go to review, a SIGKILLed worker's clip is requeued and
resumes to completion, and an injected pass-2 failure never loses the source.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
        reason="needs ffmpeg and ffprobe on PATH",
    ),
]

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


_load("pilot_common")
_load("matrix_common")
rerun_matrix = _load("rerun_matrix")


def test_synthetic_rerun_matrix_passes_end_to_end(tmp_path: Path) -> None:
    matrix = tmp_path / "matrix"
    report = tmp_path / "report.json"

    code = rerun_matrix.main(
        [
            "run",
            "--synthetic",
            "--matrix-root", str(matrix),
            "--report", str(report),
            "--seconds", "6",
            "--timeout", "180",
        ]
    )

    assert code == 0, report.read_text(encoding="utf-8") if report.exists() else "no report"
    document = json.loads(report.read_text(encoding="utf-8"))
    assert document["totals"] == {
        "scenarios": 4,
        "failed_scenarios": 0,
        "checks": document["totals"]["checks"],
        "failures": 0,
    }
    assert document["totals"]["checks"] >= 35

    scenarios = {entry["id"]: entry for entry in document["scenarios"]}
    assert set(scenarios) == {
        "rerun_skip",
        "changed_review",
        "interrupted_resume",
        "failed_recover",
    }
    assert all(entry["ok"] for entry in document["scenarios"])

    def checks(scenario_id: str) -> dict[str, bool]:
        return {check["label"]: check["ok"] for check in scenarios[scenario_id]["checks"]}

    assert checks("rerun_skip")["the completed clip still classifies complete"]
    assert checks("rerun_skip")["a later import classifies new"]
    assert checks("rerun_skip")["a rerun worker claims nothing"]
    assert checks("changed_review")["a changed completed clip classifies changed"]
    assert checks("changed_review")["the mid-run source change parks the clip in review"]
    assert checks("interrupted_resume")["the expired lease requeues the clip"]
    assert checks("interrupted_resume")["the resumed run completes the clip"]
    assert checks("failed_recover")["the injected pass-2 failure marks the job failed"]
    assert checks("failed_recover")["the retry completes the clip without losing the source"]

    assert list(document["clips"]), "the report names every staged clip"
    assert "ffmpeg version" in document["tools"]["ffmpeg"]
    text = report.read_text(encoding="utf-8")
    assert str(tmp_path) not in text
    assert "<matrix-root>" in text
    assert not matrix.exists(), "a passing run removes the matrix root unless --keep"
