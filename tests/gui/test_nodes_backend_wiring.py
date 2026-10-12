"""GUI service over the real #53 backend, driven by the fake SSH runner (issue #54).

Proves the Nodes-panel wiring reaches the real ``NodeBootstrap`` command surface
(probe, worker.toml push, install-service, uninstall-service) without any SSH
logic or network access.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fixtures.node import (
    ARCHIVE_DIR,
    CONFIG_PATH,
    CONTROLLER_URL,
    SCRATCH_DIR,
    TOKEN,
    FakeSshRunner,
    probe_output,
)
from gui.nodes_support import fake_wheelhouse, make_entry
from videocompressor.core.config import AppConfig
from videocompressor.gui import nodes_service
from videocompressor.gui.nodes_service import NodesService


@pytest.fixture
def wired(tmp_path: Path, monkeypatch):
    """A NodesService whose default factory builds a real NodeBootstrap on a fake SSH."""
    runner = FakeSshRunner()
    monkeypatch.setattr(nodes_service, "make_runner", lambda host: runner)
    monkeypatch.setattr("videocompressor.node.bootstrap.time.sleep", lambda _: None)
    config = AppConfig()
    config.controller.token = TOKEN
    service = NodesService(config, source_dir=tmp_path, wheelhouse=fake_wheelhouse(tmp_path))
    return service, runner


def test_probe_drives_the_real_backend(wired):
    service, runner = wired
    activity = service.probe(make_entry())

    assert activity.ok is True
    assert activity.summary.startswith("probe ok: Linux; Python 3.12.3; ffmpeg ok")
    assert runner.commands_with("mark version 1")
    assert not runner.commands_with("service-status")  # nothing installed yet


def test_install_pushes_the_rendered_worker_config(wired):
    service, runner = wired
    activity = service.install(make_entry(name="Test node A", thread_cap=3))

    assert activity.ok is True
    assert activity.summary == "install ok: worker node-a; service running"
    assert runner.writes and runner.writes[0][0] == CONFIG_PATH
    text = runner.writes[0][1]
    assert 'id = "node-a"' in text
    assert f'controller_url = "{CONTROLLER_URL}"' in text
    assert f'scratch_dir = "{SCRATCH_DIR}"' in text
    assert "thread_cap = 3" in text
    assert f'archive = "{ARCHIVE_DIR}"' in text
    assert f"worker install-service --config {CONFIG_PATH}" in "\n".join(runner.commands)
    assert TOKEN not in repr(activity)


def test_uninstall_with_purge_stops_the_service(wired):
    service, runner = wired
    runner.probe = probe_output(venv_present="yes", config_present="yes")
    activity = service.uninstall(make_entry(), purge=True)

    assert activity.ok is True
    assert activity.summary == "uninstall ok"
    commands = "\n".join(runner.commands)
    assert f"worker uninstall-service --config {CONFIG_PATH}" in commands
    assert f"rm -f {CONFIG_PATH}" in commands
    assert "rm -rf" in commands  # the purge


def test_probe_failure_from_the_backend_is_clean(wired):
    service, runner = wired
    runner.handlers.append(("mark version 1", 255))
    activity = service.probe(make_entry())

    assert activity.ok is False
    assert "probe failed" in activity.summary
