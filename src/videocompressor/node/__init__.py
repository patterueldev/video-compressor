"""Remote node bootstrap: manage a worker on another machine over SSH (issue #53).

The ``vc node`` CLI and the #54 Nodes panel use :class:`NodeBootstrap`; every
remote call goes through the :class:`~videocompressor.node.ssh.SshRunner` seam,
which tests replace with a fake, so no test needs network or SSH.
"""

from videocompressor.node.bootstrap import DEFAULT_INSTALL_DIR, MINIMUM_PYTHON, NodeBootstrap
from videocompressor.node.config import (
    push_worker_config,
    render_worker_toml,
    worker_config_path,
)
from videocompressor.node.distribution import Wheelhouse, prepare_wheelhouse
from videocompressor.node.models import (
    CommandResult,
    InstallError,
    InstallReport,
    NodeError,
    NodeProbe,
    ProbeError,
    RemoteHost,
    RemoteValidationError,
    ServiceError,
    SshError,
    SshTimeoutError,
    StatusReport,
    StepReport,
    UninstallReport,
    WheelhouseError,
    WorkerSpec,
)
from videocompressor.node.probe import parse_probe_output, probe_node
from videocompressor.node.service import install_service, service_status, uninstall_service
from videocompressor.node.ssh import OpenSSHRunner, SshRunner, make_runner

__all__ = [
    "DEFAULT_INSTALL_DIR",
    "MINIMUM_PYTHON",
    "CommandResult",
    "InstallError",
    "InstallReport",
    "NodeBootstrap",
    "NodeError",
    "NodeProbe",
    "OpenSSHRunner",
    "ProbeError",
    "RemoteHost",
    "RemoteValidationError",
    "ServiceError",
    "SshError",
    "SshRunner",
    "SshTimeoutError",
    "StatusReport",
    "StepReport",
    "UninstallReport",
    "Wheelhouse",
    "WheelhouseError",
    "WorkerSpec",
    "install_service",
    "make_runner",
    "parse_probe_output",
    "prepare_wheelhouse",
    "probe_node",
    "push_worker_config",
    "render_worker_toml",
    "service_status",
    "uninstall_service",
    "worker_config_path",
]
