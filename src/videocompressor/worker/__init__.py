"""Worker agent for processing nodes: one FFmpeg conversion at a time.

Implemented in issue #6. :class:`WorkerAgent` runs the register/heartbeat/claim
loop; :class:`JobPipeline` runs one claimed job through copy → encode → validate
→ replace (or pilot delivery). At most one conversion runs per node.
"""

from videocompressor.worker.agent import WorkerAgent
from videocompressor.worker.config import WorkerConfig, WorkerConfigError, load_worker_config
from videocompressor.worker.pipeline import JobOutcome, JobPipeline

__all__ = [
    "JobOutcome",
    "JobPipeline",
    "WorkerAgent",
    "WorkerConfig",
    "WorkerConfigError",
    "load_worker_config",
]
