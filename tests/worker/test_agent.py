"""Agent lifecycle: register/claim/report, heartbeat progress, stop, single job (issue #6)."""

from __future__ import annotations

import threading
import time

import pytest

from fixtures.controller import TOKEN
from fixtures.worker import CLIP, FakeEncoder
from videocompressor.core.models import Fingerprint, JobState
from videocompressor.worker.agent import WorkerAgent
from videocompressor.worker.client import ControllerAuthError, ControllerError
from videocompressor.worker.pipeline import JobOutcome, JobPipeline, PipelineSeams
from videocompressor.worker.scratch import jobs_root


class StubPipeline:
    """Pipeline double returning one fixed outcome."""

    def __init__(self, outcome: JobOutcome) -> None:
        self.outcome = outcome
        self.jobs = []

    def run(self, job, *, stop_event):
        self.jobs.append(job)
        return self.outcome


class TrackingPipeline:
    """Records how many jobs ran concurrently."""

    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0

    def run(self, job, *, stop_event):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        time.sleep(0.05)
        self.active -= 1
        return JobOutcome(
            "complete", output=Fingerprint(size_bytes=1, mtime_ns=1, digest="stub")
        )


def wait_until(predicate, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def worker_progress(api, worker_id: str = "node-a") -> dict:
    status, body = api.get("/workers")
    assert status == 200
    for entry in body["workers"]:
        if entry["worker_id"] == worker_id:
            return entry["progress"] or {}
    return {}


def test_agent_registers_claims_and_completes(
    server, api, seed_clip, manifest, worker_config_factory
):
    seed_clip(CLIP)
    config = worker_config_factory(controller_url=server.base_url, token=TOKEN)
    output = Fingerprint(size_bytes=5, mtime_ns=1, digest="out")
    pipeline = StubPipeline(JobOutcome("complete", output=output))
    agent = WorkerAgent(config, pipeline=pipeline)

    assert agent.run(max_jobs=1) == 0

    record = manifest.get_clip("archive", CLIP)
    assert record.state is JobState.COMPLETE
    assert record.output == output
    assert len(pipeline.jobs) == 1
    status, workers = api.get("/workers")
    entry = next(w for w in workers["workers"] if w["worker_id"] == config.worker_id)
    assert entry["online"] is True
    assert entry["roots"] == {"archive": str(config.roots["archive"])}
    assert entry["active"] is None


def test_heartbeat_streams_progress_and_stop_command_cancels(
    server,
    api,
    seed_clip,
    manifest,
    worker_config_factory,
    fake_prober,
    fake_validator,
    fake_recover,
    monkeypatch,
):
    seed_clip(CLIP)
    config = worker_config_factory(controller_url=server.base_url, token=TOKEN)
    source = config.roots["archive"] / CLIP
    source.parent.mkdir(parents=True)
    source.write_bytes(b"original-source")
    encoder = FakeEncoder(mode="block")

    def pipeline_factory(config, *, on_phase=None, on_progress=None):
        return JobPipeline(
            config,
            seams=PipelineSeams(
                encode=encoder,
                probe=fake_prober(),
                validate=fake_validator(),
                recover=fake_recover(),
            ),
            on_phase=on_phase,
            on_progress=on_progress,
        )

    monkeypatch.setattr("videocompressor.worker.agent.JobPipeline", pipeline_factory)
    agent = WorkerAgent(config)
    thread = threading.Thread(target=lambda: agent.run(max_jobs=1))
    thread.start()
    try:
        assert wait_until(lambda: bool(encoder.calls))
        assert wait_until(lambda: worker_progress(api).get("phase") == "encoding")
        record = manifest.get_clip("archive", CLIP)
        assert record.state is JobState.ENCODING
        progress = worker_progress(api)
        assert progress["percent"] == 50.0
        assert progress["speed"] == "1.00x"

        assert api.post("/workers/node-a/stop")[0] == 204
        assert wait_until(lambda: not thread.is_alive(), timeout=10)
    finally:
        agent.request_stop()
        thread.join(timeout=5)

    assert manifest.get_clip("archive", CLIP).state is JobState.QUEUED
    assert source.read_bytes() == b"original-source"
    assert list(jobs_root(config.scratch_dir).iterdir()) == []


def test_agent_startup_sweeps_scratch(server, worker_config_factory):
    config = worker_config_factory(controller_url=server.base_url, token=TOKEN)
    stale = jobs_root(config.scratch_dir) / "stale-job"
    stale.mkdir(parents=True)
    (stale / "source").write_bytes(b"stale")
    agent = WorkerAgent(config, pipeline=StubPipeline(JobOutcome("failed", "none")))

    assert agent.run(max_jobs=1) == 0

    assert list(jobs_root(config.scratch_dir).iterdir()) == []


def test_agent_processes_one_job_at_a_time(
    server, seed_clip, manifest, worker_config_factory
):
    seed_clip("a.MP4")
    seed_clip("b.MP4")
    config = worker_config_factory(controller_url=server.base_url, token=TOKEN)
    pipeline = TrackingPipeline()
    agent = WorkerAgent(config, pipeline=pipeline)

    assert agent.run(max_jobs=2) == 0

    assert pipeline.max_active == 1
    assert manifest.get_clip("archive", "a.MP4").state is JobState.COMPLETE
    assert manifest.get_clip("archive", "b.MP4").state is JobState.COMPLETE


class FakeClient:
    """Recording client double for registry-restart behaviour."""

    def __init__(self) -> None:
        self.registers = 0
        self.claim_calls = 0

    def register(self, **kwargs):
        self.registers += 1
        return {"heartbeat_seconds": 0.1}

    def heartbeat(self, worker_id, **kwargs):
        return {"stop": False}

    def claim(self, worker_id):
        self.claim_calls += 1
        if self.claim_calls == 1:
            raise ControllerError(404, "unknown_worker", "worker is not registered")
        return None

    def complete(self, *args, **kwargs):
        raise AssertionError("complete must not be called")

    def fail(self, *args, **kwargs):
        raise AssertionError("fail must not be called")


def test_agent_reregisters_when_the_controller_forgets_it(
    worker_config_factory, monkeypatch
):
    monkeypatch.setattr("videocompressor.worker.agent.RETRY_BACKOFF_S", 0.01)
    config = worker_config_factory()
    client = FakeClient()
    agent = WorkerAgent(
        config, client=client, pipeline=StubPipeline(JobOutcome("failed", "none"))
    )

    assert agent.run(max_jobs=1) == 0

    assert client.registers == 2
    assert client.claim_calls == 2


def test_agent_auth_failure_is_fatal(worker_config_factory):
    class AuthFailingClient(FakeClient):
        def register(self, **kwargs):
            raise ControllerAuthError(401, "unauthorized", "bad token")

    agent = WorkerAgent(
        worker_config_factory(),
        client=AuthFailingClient(),
        pipeline=StubPipeline(JobOutcome("failed", "none")),
    )
    with pytest.raises(ControllerAuthError):
        agent.run(max_jobs=1)


def test_request_stop_exits_the_idle_loop(server, worker_config_factory):
    config = worker_config_factory(controller_url=server.base_url, token=TOKEN)
    agent = WorkerAgent(config, pipeline=StubPipeline(JobOutcome("failed", "none")))
    thread = threading.Thread(target=lambda: agent.run())
    thread.start()
    try:
        agent.request_stop()
        thread.join(timeout=5)
        assert not thread.is_alive()
    finally:
        agent.request_stop()
        thread.join(timeout=5)
