"""Worker config rendering and push (issue #53)."""

from __future__ import annotations

from pathlib import Path

import pytest

from fixtures.node import (
    ARCHIVE_DIR,
    CONFIG_PATH,
    CONTROLLER_URL,
    INSTALL_DIR,
    SCRATCH_DIR,
    TOKEN,
    FakeSshRunner,
)
from videocompressor.node.config import (
    push_worker_config,
    render_worker_toml,
    worker_config_path,
)
from videocompressor.node.models import RemoteValidationError, WorkerSpec
from videocompressor.worker.config import load_worker_config


def _spec(**overrides: object) -> WorkerSpec:
    fields: dict[str, object] = {
        "controller_url": CONTROLLER_URL,
        "token": TOKEN,
        "roots": {"archive": ARCHIVE_DIR},
        "scratch_dir": SCRATCH_DIR,
        "worker_id": "node-a",
    }
    fields.update(overrides)
    return WorkerSpec(**fields)  # type: ignore[arg-type]


def test_render_round_trips_through_worker_config(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    archive.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    spec = _spec(roots={"archive": str(archive)}, scratch_dir=str(scratch), name="Node A")
    path = tmp_path / "worker.toml"
    path.write_text(render_worker_toml(spec), encoding="utf-8")
    loaded = load_worker_config(path)
    assert loaded.worker_id == "node-a"
    assert loaded.name == "Node A"
    assert loaded.controller_url == CONTROLLER_URL
    assert loaded.token == TOKEN
    assert loaded.roots == {"archive": archive.resolve()}
    assert loaded.scratch_dir == scratch.resolve()
    assert loaded.thread_cap == 2


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"worker_id": "bad id"}, "worker id"),
        ({"worker_id": None}, "worker id"),
        ({"controller_url": "http://host/with/path"}, "must not carry a path"),
        ({"controller_url": "ftp://host"}, r"http\(s\)"),
        ({"token": ""}, "token"),
        ({"roots": {}}, "at least one"),
        ({"roots": {"bad id": "/srv/x"}}, "root id"),
        ({"thread_cap": 0}, "thread cap"),
    ],
)
def test_render_rejects_invalid_specs(overrides: dict[str, object], message: str) -> None:
    with pytest.raises(RemoteValidationError, match=message):
        render_worker_toml(_spec(**overrides))


def test_worker_config_path() -> None:
    assert worker_config_path(INSTALL_DIR) == CONFIG_PATH


def test_push_creates_new_config() -> None:
    runner = FakeSshRunner(files={})
    detail = push_worker_config(runner, config_path=CONFIG_PATH, text="[worker]\n")
    assert detail == "created"
    assert runner.writes == [(CONFIG_PATH, "[worker]\n")]


def test_push_unchanged_config_skips_the_write() -> None:
    runner = FakeSshRunner(files={CONFIG_PATH: "[worker]\n"})
    assert push_worker_config(runner, config_path=CONFIG_PATH, text="[worker]\n") == "unchanged"
    assert runner.writes == []
    assert not runner.commands_with("cp -p")


def test_push_updated_config_keeps_a_backup() -> None:
    runner = FakeSshRunner(files={CONFIG_PATH: "old = true\n"})
    detail = push_worker_config(runner, config_path=CONFIG_PATH, text="new = true\n")
    assert "worker.toml.bak" in detail
    assert runner.commands_with("cp -p")
    assert runner.writes == [(CONFIG_PATH, "new = true\n")]
