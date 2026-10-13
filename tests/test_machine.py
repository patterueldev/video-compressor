"""Machine identity helper shared by the worker agent and the GUI (issue #80)."""

from __future__ import annotations

from videocompressor.core import machine as machine_module
from videocompressor.core.machine import machine_identity


def test_machine_identity_is_the_stripped_hostname(monkeypatch):
    monkeypatch.setattr(machine_module.socket, "gethostname", lambda: " node-a ")
    assert machine_identity() == "node-a"


def test_machine_identity_is_none_when_empty(monkeypatch):
    monkeypatch.setattr(machine_module.socket, "gethostname", lambda: "")
    assert machine_identity() is None


def test_machine_identity_is_none_without_a_hostname(monkeypatch):
    def failing() -> str:
        raise OSError("no hostname")

    monkeypatch.setattr(machine_module.socket, "gethostname", failing)
    assert machine_identity() is None
