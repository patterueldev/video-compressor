#!/usr/bin/env python3
"""Replacement-protocol drill for issue #5.

Exercises the verified replacement protocol and every crash/recovery path against a
real clip, printing a transcript suitable for a PR. By default the drill runs in a
fresh ``vc-drill-<timestamp>`` workdir under ``--target`` (default ``~/Temp`` on the
local filesystem); point ``--target`` at a scratch folder on an SMB mount to run the
same temp-write + rename dance over SMB. The source clip is only ever copied into
the workdir, and the workdir is deleted afterwards unless ``--keep`` is passed.

Run it from the project virtualenv (``pip install -e ".[dev]"``).
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import time
from pathlib import Path

from videocompressor.core.models import Fingerprint
from videocompressor.core.recover import RecoveryOutcome, reconcile_source
from videocompressor.core.replace import (
    ffprobe_readable,
    find_artifacts,
    replace_with_verified_copy,
)

CRASH_EXPECTATIONS = {
    "temp_written": RecoveryOutcome.TEMP_REMOVED,
    "temp_verified": RecoveryOutcome.TEMP_REMOVED,
    "source_verified": RecoveryOutcome.TEMP_REMOVED,
    "backup_renamed": RecoveryOutcome.ORIGINAL_RESTORED,
    "swapped": RecoveryOutcome.REPLACEMENT_CONFIRMED,
    "confirmed": RecoveryOutcome.REPLACEMENT_CONFIRMED,
    "backup_deleted": RecoveryOutcome.NOTHING_TO_DO,
}


class InjectedCrash(RuntimeError):
    """Simulates a process death at one protocol step."""


class Transcript:
    """Collects PASS/FAIL lines and the overall failure count."""

    def __init__(self) -> None:
        self.checks = 0
        self.failures = 0

    def note(self, message: str) -> None:
        print(f"  {message}")

    def check(self, label: str, condition: bool, detail: str = "") -> None:
        self.checks += 1
        status = "PASS" if condition else "FAIL"
        suffix = f" ({detail})" if detail else ""
        print(f"  [{status}] {label}{suffix}")
        if not condition:
            self.failures += 1


def fingerprint(path: Path) -> Fingerprint:
    """Stand-in for issue #3's sampled fingerprint: size + mtime + full SHA-256."""
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
            size += len(chunk)
    return Fingerprint(
        size_bytes=size,
        mtime_ns=path.stat().st_mtime_ns,
        digest=digest.hexdigest(),
    )


def _resolve_clip(explicit: Path | None) -> Path:
    if explicit is not None:
        if not explicit.is_file():
            raise SystemExit(f"clip not found: {explicit}")
        return explicit
    candidates = [
        path
        for path in (Path.home() / "Temp").glob("*.mp4")
        if path.stat().st_size > 1_000_000
    ]
    if not candidates:
        raise SystemExit("no .mp4 clip found in ~/Temp; pass --clip")
    gopro = [path for path in candidates if path.name.upper().startswith("GX")]
    return min(gopro or candidates, key=lambda path: path.stat().st_size)


def _remux(seed: Path, variant: Path) -> None:
    """Cut a short variant of the clip so the replacement differs in bytes."""
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-i",
            str(seed),
            "-t",
            "5",
            "-c",
            "copy",
            str(variant),
        ],
        check=True,
    )


def _describe(directory: Path) -> str:
    entries = sorted(entry.name for entry in directory.iterdir() if entry.is_file())
    if not entries:
        return "no files"
    return ", ".join(
        f"{name} ({directory.joinpath(name).stat().st_size} B)" for name in entries
    )


def run_happy_path(workdir: Path, seed: Path, variant: Path, transcript: Transcript) -> None:
    print("\n== happy path: pre-flight -> stage -> backup -> swap -> ffprobe -> cleanup")
    source = workdir / "GS_DRILL_0001.MP4"
    local_output = workdir / "GS_DRILL_0001.local-output.MP4"
    shutil.copyfile(seed, source)
    shutil.copyfile(variant, local_output)
    expected = fingerprint(source)

    result = replace_with_verified_copy(
        local_output,
        source,
        expected_source=expected,
        job_id="drill-happy",
        fingerprint=fingerprint,
        on_step=lambda step: transcript.note(f"step: {step}"),
    )

    transcript.check(
        "source now holds the encoded output",
        source.read_bytes() == local_output.read_bytes(),
    )
    transcript.check("replacement confirmed readable by ffprobe", ffprobe_readable(source))
    transcript.check("no swap artifacts left behind", not find_artifacts(source).all)
    transcript.check(
        "reported output fingerprint matches the file", result.output == fingerprint(source)
    )
    transcript.check("backup was removed", result.backup_removed)


def run_crash_matrix(
    workdir: Path, seed: Path, variant: Path, transcript: Transcript
) -> None:
    print("\n== crash matrix: die at each step, then reconcile")
    for index, (step, expected_outcome) in enumerate(CRASH_EXPECTATIONS.items()):
        print(f"\n-- crash after '{step}'")
        crash_dir = workdir / f"crash-{index:02d}-{step}"
        crash_dir.mkdir()
        source = crash_dir / "GS_DRILL_0002.MP4"
        local_output = crash_dir / "local-output.MP4"
        shutil.copyfile(seed, source)
        shutil.copyfile(variant, local_output)
        original_bytes = source.read_bytes()
        replacement_bytes = local_output.read_bytes()
        expected = fingerprint(source)

        def crash_hook(hooked_step: str, crash_step: str = step) -> None:
            if hooked_step == crash_step:
                raise InjectedCrash(hooked_step)

        try:
            replace_with_verified_copy(
                local_output,
                source,
                expected_source=expected,
                job_id=f"drill-{index}",
                fingerprint=fingerprint,
                on_step=crash_hook,
            )
            crashed = False
        except InjectedCrash:
            crashed = True

        transcript.check(f"protocol died at '{step}' as injected", crashed)
        transcript.note(f"on disk: {_describe(crash_dir)}")

        report = reconcile_source(source, confirm=ffprobe_readable)
        transcript.check(
            f"recovery outcome is {expected_outcome}",
            report.outcome is expected_outcome,
            f"got {report.outcome}",
        )
        transcript.check("source path exists again", source.exists())
        transcript.check(
            "source holds the original or the confirmed replacement",
            source.exists()
            and source.read_bytes() in (original_bytes, replacement_bytes),
        )
        transcript.check("no swap artifacts remain", not find_artifacts(source).all)

        second = reconcile_source(source, confirm=ffprobe_readable)
        transcript.check(
            "second recovery run is a no-op",
            second.outcome is RecoveryOutcome.NOTHING_TO_DO,
            f"got {second.outcome}",
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--target",
        type=Path,
        default=Path.home() / "Temp",
        help="parent directory for the drill workdir (default: ~/Temp)",
    )
    parser.add_argument(
        "--clip", type=Path, default=None, help="clip to use as scratch (default: ~/Temp)"
    )
    parser.add_argument(
        "--keep", action="store_true", help="keep the drill workdir for inspection"
    )
    args = parser.parse_args(argv)

    clip = _resolve_clip(args.clip)
    target = args.target.expanduser()
    workdir = target / f"vc-drill-{time.strftime('%Y%m%d-%H%M%S')}"

    print("Video Compressor replacement drill (issue #5)")
    print(f"time:   {time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"clip:   {clip} ({clip.stat().st_size / 1e6:.1f} MB, read-only input)")
    print(f"target: {workdir}")
    print(f"ffmpeg: {shutil.which('ffmpeg')}")
    print(f"ffprobe: {shutil.which('ffprobe')}")

    workdir.mkdir(parents=True)
    transcript = Transcript()
    try:
        seed = workdir / "seed.MP4"
        variant = workdir / "variant.MP4"
        shutil.copyfile(clip, seed)
        _remux(seed, variant)
        run_happy_path(workdir, seed, variant, transcript)
        run_crash_matrix(workdir, seed, variant, transcript)
    finally:
        if args.keep:
            print(f"\nkept workdir: {workdir}")
        else:
            shutil.rmtree(workdir)
            print(f"\nremoved workdir: {workdir}")

    print(f"\n{transcript.checks} checks, {transcript.failures} failures")
    if transcript.failures:
        print("DRILL FAILED")
        return 1
    print("DRILL PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
