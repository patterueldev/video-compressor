"""Deterministic two-node concurrency check with injectable pipeline seams (issue #10).

Two agents share one controller and one archive but have separate scratch dirs. The fake
encode seam waits on a barrier, so both nodes must be encoding different clips at the same
time before either can finish: this makes the "two nodes simultaneously on different clips,
one job per node, no duplicate claims" acceptance property deterministic and ffmpeg-free.
"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path

from videocompressor.core.ffmpeg import EncodeResult
from videocompressor.core.models import Fingerprint, JobState
from videocompressor.core.replace import ReplaceResult
from videocompressor.worker.agent import WorkerAgent
from videocompressor.worker.pipeline import JobPipeline, PipelineSeams

CLIPS = ("ride-a/GS010001.MP4", "ride-b/GS010002.MP4")


def fingerprint(path: Path) -> Fingerprint:
    """Cheap content fingerprint for the fake pipeline seams."""
    data = path.read_bytes()
    return Fingerprint(
        size_bytes=len(data),
        mtime_ns=path.stat().st_mtime_ns,
        digest=hashlib.sha256(data).hexdigest(),
    )


def test_two_nodes_encode_different_clips_concurrently(
    server,
    manifest,
    seed_clip,
    worker_config_factory,
    fake_prober,
    fake_validator,
    fake_recover,
):
    config_a = worker_config_factory(worker_id="node-a", controller_url=server.base_url)
    config_b = worker_config_factory(
        worker_id="node-b",
        controller_url=server.base_url,
        scratch_dir=config_a.scratch_dir.parent / "scratch-b",
    )
    archive = config_a.roots["archive"]
    for relative in CLIPS:
        source = archive / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(f"source-{relative}".encode())
        seed_clip(relative)

    barrier = threading.Barrier(2, timeout=30)
    encoders: list[str] = []

    def fake_encode(source: Path, output: Path, settings, **kwargs) -> EncodeResult:
        encoders.append(threading.current_thread().name)
        barrier.wait()  # both nodes must be encoding before either proceeds
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"fake-encoded-" + source.name.encode())
        return EncodeResult(
            output_path=output,
            output_bytes=output.stat().st_size,
            pass_seconds=(0.1, 0.2),
            final_progress=(None, None),
        )

    def fake_replace(output, source, *, expected_source, job_id, fingerprint, confirm, on_step):
        source.write_bytes(output.read_bytes())
        return ReplaceResult(
            source=source, job_id=job_id, output=fingerprint(source), backup_removed=True
        )

    seams = PipelineSeams(
        encode=fake_encode,
        probe=fake_prober(),
        fingerprint=fingerprint,
        replace=fake_replace,
        validate=fake_validator(),
        recover=fake_recover(),
    )
    agents = [
        WorkerAgent(config, pipeline=JobPipeline(config, seams=seams))
        for config in (config_a, config_b)
    ]
    threads = [
        threading.Thread(target=agent.run, kwargs={"max_jobs": 1}, name=agent.config.worker_id)
        for agent in agents
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive(), f"{thread.name} did not exit"

    assert len(encoders) == 2, "both nodes must reach the encode seam at the same time"
    records = {relative: manifest.get_clip("archive", relative) for relative in CLIPS}
    assert all(
        record is not None and record.state is JobState.COMPLETE for record in records.values()
    ), records
    assert {record.worker_id for record in records.values()} == {"node-a", "node-b"}

    claim_events = [
        event
        for event in server.events()["events"]
        if event["message"].startswith("claimed ")
    ]
    assert sorted(event["message"] for event in claim_events) == sorted(
        f"claimed {relative}" for relative in CLIPS
    )
    assert {event["worker_id"] for event in claim_events} == {"node-a", "node-b"}
