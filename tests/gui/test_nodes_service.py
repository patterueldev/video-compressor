"""Nodes service seam: spec building, activity formatting, registry merge (issue #54)."""

from __future__ import annotations

import pytest

from gui.monitor_support import worker_payload, workers_payload
from gui.nodes_support import (
    ARCHIVE,
    CONTROLLER_URL,
    SCRATCH,
    TOKEN,
    FakeBootstrap,
    FakeBootstrapFactory,
    FakeRegistryClient,
    make_entry,
    make_probe,
)
from videocompressor.core.config import AppConfig
from videocompressor.gui import nodes_service
from videocompressor.gui.nodes_service import (
    NodesService,
    default_controller_url,
    remote_host,
)
from videocompressor.node import DEFAULT_INSTALL_DIR
from videocompressor.node.models import ProbeError, SshError, WorkerSpec


@pytest.fixture
def config() -> AppConfig:
    app_config = AppConfig()
    app_config.controller.host = "controller.example.com"
    app_config.controller.port = 8765
    app_config.controller.token = TOKEN
    return app_config


def _service(config: AppConfig, **kwargs: object) -> NodesService:
    factory = kwargs.pop("bootstrap_factory", None) or FakeBootstrapFactory()
    return NodesService(config, bootstrap_factory=factory, **kwargs)  # type: ignore[arg-type]


def test_remote_host_maps_the_ssh_fields():
    host = remote_host(make_entry())
    assert host.host == "192.0.2.10"
    assert host.user == "example"
    assert host.port == 22
    assert host.identity == "~/.ssh/id_ed25519"
    assert host.strict_host_keys is False

    bare = remote_host(make_entry(user="", port=None, identity="", strict_host_keys=True))
    assert bare.user is None
    assert bare.port is None
    assert bare.identity is None
    assert bare.strict_host_keys is True


def test_default_controller_url_skips_loopback_and_wildcard(config):
    assert default_controller_url(config) == "http://controller.example.com:8765"
    for host in ("127.0.0.1", "localhost", "0.0.0.0", "::", ""):
        config.controller.host = host
        assert default_controller_url(config) == ""


def test_worker_spec_carries_every_install_field(config):
    spec = _service(config).worker_spec(make_entry())
    assert spec == WorkerSpec(
        controller_url=CONTROLLER_URL,
        token=TOKEN,
        roots={"archive": ARCHIVE},
        scratch_dir=SCRATCH,
        worker_id="node-a",
        name="Test node A",
        thread_cap=2,
        poll_seconds=5.0,
    )


@pytest.mark.parametrize("overrides", [{"controller_url": ""}, {"scratch_dir": ""}, {"roots": {}}])
def test_worker_spec_requires_the_install_fields(config, overrides):
    with pytest.raises(ValueError, match="set "):
        _service(config).worker_spec(make_entry(**overrides))


def test_worker_spec_requires_the_controller_token(config):
    config.controller.token = None
    with pytest.raises(ValueError, match="token"):
        _service(config).worker_spec(make_entry())


def test_probe_formats_the_report_and_uses_the_factory(config):
    factory = FakeBootstrapFactory(
        FakeBootstrap(
            probe=make_probe(
                venv_present=True,
                config_present=True,
                worker_version="video-compressor 0.1.0",
                service_status="worker service: running",
                service_ok=True,
            )
        )
    )
    activity = _service(config, bootstrap_factory=factory).probe(make_entry())

    assert activity.ok is True
    assert activity.summary == (
        "probe ok: Linux; Python 3.12.3; ffmpeg ok; service running"
    )
    assert any(line.startswith("worker: video-compressor 0.1.0") for line in activity.lines)
    assert any(line.startswith("service: running") for line in activity.lines)
    assert factory.entries == [make_entry()]


def test_probe_failure_becomes_a_failed_activity(config):
    factory = FakeBootstrapFactory(
        FakeBootstrap(error=ProbeError("remote probe failed: no markers"))
    )
    activity = _service(config, bootstrap_factory=factory).probe(make_entry())

    assert activity.ok is False
    assert activity.error == "remote probe failed: no markers"
    assert activity.summary == "probe failed: remote probe failed: no markers"


def test_install_builds_the_spec_and_formats_the_report(config):
    bootstrap = FakeBootstrap()
    activity = _service(
        config, bootstrap_factory=FakeBootstrapFactory(bootstrap)
    ).install(make_entry())

    assert activity.ok is True
    assert activity.summary == "install ok: worker node-a; service running"
    assert bootstrap.specs == [_service(config).worker_spec(make_entry())]
    assert any("config: ok" in line for line in activity.lines)
    assert TOKEN not in activity.summary
    assert all(TOKEN not in line for line in activity.lines)


def test_install_missing_fields_return_a_failed_activity(config):
    factory = FakeBootstrapFactory()
    activity = _service(config, bootstrap_factory=factory).install(make_entry(controller_url=""))

    assert activity.ok is False
    assert "controller URL" in (activity.error or "")
    assert factory.entries == []


def test_install_backend_error_is_reported_without_raising(config):
    factory = FakeBootstrapFactory(FakeBootstrap(error=SshError("ssh failed: connection refused")))
    activity = _service(config, bootstrap_factory=factory).install(make_entry())

    assert activity.ok is False
    assert activity.summary == "install failed: ssh failed: connection refused"


def test_uninstall_passes_purge_and_formats_the_steps(config):
    bootstrap = FakeBootstrap()
    activity = _service(
        config, bootstrap_factory=FakeBootstrapFactory(bootstrap)
    ).uninstall(make_entry(), purge=True)

    assert activity.ok is True
    assert activity.summary == "uninstall ok"
    assert bootstrap.purges == [True]
    assert any("service-uninstall: ok" in line for line in activity.lines)


def test_registry_online_reads_the_controller_snapshot(config):
    client = FakeRegistryClient(
        workers_payload(
            worker_payload("node-a", online=True),
            worker_payload("node-b", online=False),
        )
    )
    service = _service(config, client=client)
    assert service.registry_online() == {"node-a"}
    assert client.calls == 1


def test_registry_online_fails_soft(config):
    assert _service(config).registry_online() is None  # no client wired
    client = FakeRegistryClient(error=RuntimeError("controller down"))
    assert _service(config, client=client).registry_online() is None


def test_default_factory_wires_the_node_bootstrap(monkeypatch, tmp_path):
    captured: dict[str, object] = {}
    runner = object()

    def fake_make_runner(host):
        captured["host"] = host
        return runner

    def fake_bootstrap(actual_runner, host, **kwargs):
        captured["runner"] = actual_runner
        captured["kwargs"] = kwargs
        return FakeBootstrap()

    monkeypatch.setattr(nodes_service, "make_runner", fake_make_runner)
    monkeypatch.setattr(nodes_service, "NodeBootstrap", fake_bootstrap)
    service = NodesService(config, source_dir=tmp_path, wheelhouse=tmp_path)

    entry = make_entry()
    assert service.probe(entry).ok is True
    assert captured["host"] == remote_host(entry)
    assert captured["runner"] is runner
    assert captured["kwargs"] == {
        "install_dir": DEFAULT_INSTALL_DIR,
        "source_dir": tmp_path,
        "wheelhouse": tmp_path,
    }

    custom = make_entry(id="node-c", install_dir="~/tools/vc")
    service.probe(custom)
    assert captured["kwargs"]["install_dir"] == "~/tools/vc"  # type: ignore[index]


def test_activity_objects_never_carry_the_token(config):
    bootstrap = FakeBootstrap()
    factory = FakeBootstrapFactory(bootstrap)
    service = _service(config, bootstrap_factory=factory)
    for activity in (service.probe(make_entry()), service.install(make_entry())):
        assert TOKEN not in repr(activity)
