#!/usr/bin/env python3
"""Local two-node concurrency check for the homeserver worker deployment (issue #10).

Runs one native node and one container node against a single controller on two generated
clips and verifies the plan.md concurrency rules: both nodes encode simultaneously on
different clips, each clip is claimed exactly once by a distinct worker, both complete with
HEVC 720p output and recorded fingerprints, and no replacement artifacts are left behind.

Run it from the project virtualenv on a machine with ffmpeg and Docker; the real homeserver
run is an operator checkpoint (see deploy/homeserver/README.md). Docker bind mounts must be
able to see the workdir, so on macOS keep ``--target`` under the home directory.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from videocompressor.controller.server import ControllerServer, new_token, open_manifest
from videocompressor.controller.store import ControllerStore
from videocompressor.core.fingerprint import fingerprint_file
from videocompressor.core.models import ClipRecord, JobState
from videocompressor.core.probe import probe_media
from videocompressor.core.replace import find_artifacts

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = REPO_ROOT / "deploy" / "homeserver" / "Dockerfile"
CONFIG_TEMPLATE = REPO_ROOT / "deploy" / "homeserver" / "worker.example.toml"

CLIPS = ("ride-a/GS010001.MP4", "ride-b/GS010002.MP4")
CONTAINER_NAME = "vc-two-node-container"
CHECK_TIMEOUT_S = 300.0
POLL_SECONDS = 0.5

#: Runs inside a container to prove it can reach the controller (any HTTP answer counts).
_PROBE = """
import urllib.error, urllib.request
try:
    urllib.request.urlopen("http://{alias}:{port}/api/v1/events", timeout=5)
    print("REACHABLE")
except urllib.error.HTTPError:
    print("REACHABLE")  # 401 still proves the controller answered
except Exception as exc:
    print("UNREACHABLE:", exc)
"""


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


def make_clip(path: Path, seconds: int) -> None:
    """Generate one small test clip with video and audio."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", f"testsrc2=size=640x480:rate=15:duration={seconds}",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
            "-c:v", "mpeg4", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def render_config(template: Path, replacements: dict[str, str]) -> str:
    """Render the worker config template by substituting placeholder strings."""
    text = template.read_text(encoding="utf-8")
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text


def local_ipv4_addresses() -> list[str]:
    """Best-effort list of this host's non-loopback IPv4 addresses."""
    addresses: list[str] = []
    try:
        if sys.platform == "darwin":
            output = subprocess.run(["ifconfig"], capture_output=True, text=True).stdout
            addresses = [
                line.split()[1]
                for line in output.splitlines()
                if line.strip().startswith("inet ")
            ]
        else:
            output = subprocess.run(["hostname", "-I"], capture_output=True, text=True).stdout
            addresses = output.split()
    except OSError:
        return []
    return [address for address in dict.fromkeys(addresses) if not address.startswith("127.")]


def container_can_reach(alias: str, add_host: str | None, port: int, image: str) -> bool:
    """Run a throwaway container and check it can reach ``alias``:``port``."""
    command = ["docker", "run", "--rm"]
    if add_host:
        command += ["--add-host", add_host]
    command += ["--entrypoint", "python3", image, "-c", _PROBE.format(alias=alias, port=port)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and "REACHABLE" in result.stdout


def detect_host_add_host(port: int, image: str, transcript: Transcript) -> str | None:
    """Find how containers reach this host; ``""`` means the built-in alias already works.

    Returns ``None`` when no candidate worked.
    """
    candidates: list[tuple[str, str | None]] = [
        ("host.docker.internal", None),  # Docker Desktop maps the alias already
        ("host.docker.internal", "host.docker.internal:host-gateway"),  # daemon on this host
    ]
    for address in local_ipv4_addresses():
        candidates.append(("host.docker.internal", f"host.docker.internal:{address}"))
    for alias, add_host in candidates:
        if container_can_reach(alias, add_host, port, image):
            transcript.note(f"container route to this host: {add_host or alias + ' (built-in)'}")
            return add_host or ""
    return None


def api_get(base_url: str, token: str, path: str) -> dict:
    """GET one controller endpoint with the bearer token."""
    request = urllib.request.Request(base_url + path, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read())


def run_check(args: argparse.Namespace, transcript: Transcript) -> Path:
    """Run the two-node check and return the workdir (logs included)."""
    target = args.target.expanduser()
    target.mkdir(parents=True, exist_ok=True)
    workdir = target / f"vc-two-node-{time.strftime('%Y%m%d-%H%M%S')}"
    workdir.mkdir(parents=True)
    print(f"workdir: {workdir}")
    print(f"ffmpeg:  {shutil.which('ffmpeg')}")

    archive = workdir / "archive"
    scratch_native = workdir / "scratch-native"
    scratch_container = workdir / "scratch-container"
    for directory in (archive, scratch_native, scratch_container):
        directory.mkdir()
    for relative in CLIPS:
        make_clip(archive / relative, seconds=args.seconds)

    token = new_token()
    manifest, durable = open_manifest(workdir / "manifest.sqlite")
    transcript.check("durable manifest opened", durable)
    store = ControllerStore.open(workdir / "controller.sqlite")
    server = ControllerServer(store=store, manifest=manifest, token=token, host="0.0.0.0", port=0)
    server.start_background()
    port = server.port
    base_url = f"http://127.0.0.1:{port}/api/v1"
    print(f"controller: {server.base_url}")

    now = datetime.now(UTC)
    for relative in CLIPS:
        manifest.upsert_clip(
            ClipRecord(
                root_id="archive",
                relative_path=relative,
                source=fingerprint_file(archive / relative),
                state=JobState.QUEUED,
                created_at=now,
                updated_at=now,
            )
        )

    native_config = workdir / "node-native.toml"
    native_config.write_text(
        render_config(
            CONFIG_TEMPLATE,
            {
                'id = "homeserver"': 'id = "node-native"',
                "http://controller-host:8765": f"http://127.0.0.1:{port}",
                "/srv/archive": str(archive),
                "/var/lib/videocompressor/scratch": str(scratch_native),
            },
        ),
        encoding="utf-8",
    )
    container_config = workdir / "node-container.toml"
    container_config.write_text(
        render_config(
            CONFIG_TEMPLATE,
            {
                'id = "homeserver"': 'id = "node-container"',
                "http://controller-host:8765": f"http://host.docker.internal:{port}",
            },
        ),
        encoding="utf-8",
    )

    native_log = (workdir / "node-native.log").open("w", encoding="utf-8")
    native: subprocess.Popen | None = None
    container_started = False
    states: dict[str, JobState | None] = {}
    overlap = False
    events: list = []
    try:
        native = subprocess.Popen(
            [sys.executable, "-m", "videocompressor.cli", "worker", "--config", str(native_config)],
            env={**os.environ, "VC_WORKER_TOKEN": token},
            stdout=native_log,
            stderr=subprocess.STDOUT,
        )
        transcript.note(f"native node pid {native.pid}")

        container_ready = False
        if not args.skip_container:
            if not args.no_build:
                build = subprocess.run(
                    ["docker", "build", "-f", str(DOCKERFILE), "-t", args.image, str(REPO_ROOT)],
                    capture_output=True,
                    text=True,
                )
                tail = (build.stderr or "").strip().splitlines()
                transcript.check(
                    "worker image built", build.returncode == 0, tail[-1] if tail else ""
                )
                container_ready = build.returncode == 0
            else:
                container_ready = True
            if container_ready:
                add_host = detect_host_add_host(port, args.image, transcript)
                if add_host is None:
                    transcript.check(
                        "containers can reach this host",
                        False,
                        "no candidate address worked; rerun with --skip-container if Docker "
                        "cannot reach this machine",
                    )
                else:
                    subprocess.run(["docker", "rm", "-f", CONTAINER_NAME], capture_output=True)
                    run_command = ["docker", "run", "-d", "--name", CONTAINER_NAME]
                    if add_host:
                        run_command += ["--add-host", add_host]
                    run_command += [
                        "--user", f"{os.getuid()}:{os.getgid()}",
                        "-e", f"VC_WORKER_TOKEN={token}",
                        "-v", f"{container_config}:/config/worker.toml:ro",
                        "-v", f"{archive}:/srv/archive:rw",
                        "-v", f"{scratch_container}:/var/lib/videocompressor/scratch",
                        args.image,
                    ]
                    started = subprocess.run(run_command, capture_output=True, text=True)
                    tail = (started.stderr or "").strip().splitlines()
                    container_started = started.returncode == 0
                    transcript.check(
                        "container node started",
                        container_started,
                        tail[-1] if tail else "",
                    )

        deadline = time.monotonic() + CHECK_TIMEOUT_S
        while time.monotonic() < deadline:
            workers = api_get(base_url, token, "/workers")["workers"]
            active_workers = [worker for worker in workers if worker.get("active") is not None]
            active = {worker["worker_id"]: worker["active"] for worker in active_workers}
            phases = {
                worker["worker_id"]: (worker.get("progress") or {}).get("phase")
                for worker in active_workers
            }
            clips = {worker["active"]["relative_path"] for worker in active_workers}
            if (
                len(active) >= 2
                and len(clips) == len(active)
                and all(phase == "encoding" for phase in phases.values())
            ):
                overlap = True
            states = {}
            for relative in CLIPS:
                record = manifest.get_clip("archive", relative)
                states[relative] = record.state if record is not None else None
            if all(state is JobState.COMPLETE for state in states.values()):
                break
            time.sleep(POLL_SECONDS)
    finally:
        if native is not None:
            native.send_signal(signal.SIGTERM)
            try:
                native.wait(timeout=60)
            except subprocess.TimeoutExpired:
                native.kill()
                native.wait()
        native_log.close()
        if container_started:
            subprocess.run(["docker", "stop", CONTAINER_NAME], capture_output=True)
            logs = subprocess.run(
                ["docker", "logs", CONTAINER_NAME], capture_output=True, text=True
            )
            (workdir / "node-container.log").write_text(
                logs.stdout + logs.stderr, encoding="utf-8"
            )
            subprocess.run(["docker", "rm", "-f", CONTAINER_NAME], capture_output=True)
        events = store.list_events(limit=500)
        server.stop()

    print("\nresults:")
    transcript.check(
        "both clips complete",
        all(state is JobState.COMPLETE for state in states.values()),
        str(states),
    )
    transcript.check("two nodes encoded different clips simultaneously", overlap)

    claimed: dict[str, list[str]] = {relative: [] for relative in CLIPS}
    for event in events:
        if event.message.startswith("claimed "):
            relative = event.message.removeprefix("claimed ")
            if relative in claimed:
                claimed[relative].append(event.worker_id or "?")
    for relative in CLIPS:
        transcript.check(
            f"{relative} claimed exactly once", len(claimed[relative]) == 1, str(claimed[relative])
        )
    claimers = {worker for workers in claimed.values() for worker in workers}
    transcript.check("claims came from distinct workers", len(claimers) == 2, str(claimers))

    native_lines = (workdir / "node-native.log").read_text(encoding="utf-8").splitlines()
    settled_native = [line for line in native_lines if "settled: complete" in line]
    transcript.check("native node settled exactly one job", len(settled_native) == 1)

    if container_started:
        container_text = (workdir / "node-container.log").read_text(encoding="utf-8")
        settled_container = [
            line for line in container_text.splitlines() if "settled: complete" in line
        ]
        transcript.check("container node settled exactly one job", len(settled_container) == 1)
        transcript.check(
            "container registered with the controller", "registered worker" in container_text
        )

    for relative in CLIPS:
        record = manifest.get_clip("archive", relative)
        facts = probe_media(archive / relative)
        video = next((stream for stream in facts.streams if stream.codec_type == "video"), None)
        transcript.check(
            f"{relative} is HEVC 720p",
            video is not None and video.codec_name == "hevc" and video.height == 720,
            f"{video.codec_name} {video.width}x{video.height}" if video is not None else "no video",
        )
        transcript.check(
            f"{relative} output fingerprint recorded",
            record is not None and record.output == fingerprint_file(archive / relative),
        )
        transcript.check(
            f"{relative} has no swap artifacts", find_artifacts(archive / relative).all == ()
        )

    print("\nlog excerpts:")
    for name in ("node-native.log", "node-container.log"):
        path = workdir / name
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines()[-4:]:
                print(f"  {name}: {line}")
    manifest.close()
    return workdir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--target",
        type=Path,
        default=Path.home() / "Temp",
        help="parent directory for the workdir (default: ~/Temp; must be Docker-bindable)",
    )
    parser.add_argument(
        "--image",
        default="video-compressor-worker:latest",
        help="worker image tag to build and run (default: video-compressor-worker:latest)",
    )
    parser.add_argument("--no-build", action="store_true", help="skip the docker build step")
    parser.add_argument(
        "--skip-container",
        action="store_true",
        help="run only the native node (no Docker required)",
    )
    parser.add_argument("--keep", action="store_true", help="keep the workdir and node logs")
    parser.add_argument(
        "--seconds", type=int, default=10, help="length of each generated clip (default: 10)"
    )
    args = parser.parse_args(argv)

    for binary in ("ffmpeg", "ffprobe"):
        if shutil.which(binary) is None:
            print(f"vc two-node check: {binary} is required on PATH", file=sys.stderr)
            return 2
    if not args.skip_container and shutil.which("docker") is None:
        print(
            "vc two-node check: docker is required for the container leg "
            "(use --skip-container to run the native node only)",
            file=sys.stderr,
        )
        return 2

    print("Video Compressor two-node check (issue #10)")
    transcript = Transcript()
    workdir = run_check(args, transcript)

    print(f"\n{transcript.checks} checks, {transcript.failures} failures")
    if args.keep:
        print(f"kept workdir: {workdir}")
    else:
        shutil.rmtree(workdir, ignore_errors=True)
        print(f"removed workdir: {workdir}")
    if transcript.failures:
        print("TWO-NODE CHECK FAILED")
        return 1
    print("TWO-NODE CHECK PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
