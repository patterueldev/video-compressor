"""Local-worker settings and generated config (issue #52).

Pure Python — runs without the optional PySide6 extra.
"""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

from gui.local_worker_support import make_config
from videocompressor.core.config import (
    AppConfig,
    LocalWorkerSettings,
    load_config,
    save_config,
    validate_local_worker,
)
from videocompressor.gui.local_worker import (
    build_local_worker_toml,
    local_worker_toml_path,
    service_command,
    worker_command,
)
from videocompressor.gui.local_worker_config import TOKEN_ENV_VAR
from videocompressor.worker.config import load_worker_config
from videocompressor.worker.service import default_worker_command

# -- settings -----------------------------------------------------------------


def test_local_worker_defaults_round_trip(tmp_path):
    config = AppConfig()
    path = save_config(config, tmp_path / "config.toml")

    text = path.read_text(encoding="utf-8")
    assert "[local_worker]" in text
    assert 'id = "local"' in text
    assert "enabled = true" in text
    assert "keep_running = false" in text
    assert load_config(path).local_worker == LocalWorkerSettings()


def test_missing_local_worker_section_loads_defaults(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[capture_time]\nmode = "utc"\n', encoding="utf-8")
    assert load_config(path).local_worker == LocalWorkerSettings()


def test_ill_typed_local_worker_section_falls_back_per_key(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        "[local_worker]\n"
        "enabled = false\n"
        "id = 7\n"
        "keep_running = 1\n"
        'thread_cap = "many"\n'
        "scratch_dir = 12\n",
        encoding="utf-8",
    )
    settings = load_config(path).local_worker
    assert settings.enabled is False
    assert settings.worker_id == "local"
    assert settings.keep_running is False
    assert settings.thread_cap == 2
    assert settings.scratch_dir is None


def test_local_worker_round_trips_a_custom_scratch_and_id(tmp_path):
    config = AppConfig()
    config.local_worker = LocalWorkerSettings(
        enabled=False,
        worker_id="local-2",
        keep_running=True,
        scratch_dir=tmp_path / "scratch",
        thread_cap=4,
    )
    path = save_config(config, tmp_path / "config.toml")
    assert load_config(path).local_worker == config.local_worker


def test_validate_local_worker_accepts_defaults():
    assert validate_local_worker(LocalWorkerSettings()) == []


def test_validate_local_worker_reports_every_problem(tmp_path):
    config = make_config(tmp_path)
    settings = LocalWorkerSettings(
        worker_id="bad id",
        scratch_dir=config.roots[0].path / "nested",
        thread_cap=0,
    )
    problems = validate_local_worker(settings, config.roots)
    assert len(problems) == 3
    assert any("worker id" in problem for problem in problems)
    assert any("positive" in problem for problem in problems)
    assert any("registered root" in problem for problem in problems)


def test_validate_local_worker_rejects_a_relative_scratch_dir():
    problems = validate_local_worker(LocalWorkerSettings(scratch_dir=Path("scratch/x")))
    assert problems == ["scratch dir must be an absolute path"]


# -- generated config ---------------------------------------------------------


def test_build_local_worker_toml_maps_roots_and_wildcard_bind(tmp_path):
    config = make_config(tmp_path, host="0.0.0.0", port=9000, thread_cap=3)
    document = tomllib.loads(
        build_local_worker_toml(
            config, worker_config_path=local_worker_toml_path(tmp_path / "config.toml")
        ).decode("utf-8")
    )
    assert document["worker"] == {
        "id": "local",
        "name": "Local worker",
        "controller_url": "http://127.0.0.1:9000",
        "token_env": TOKEN_ENV_VAR,
        "scratch_dir": str(tmp_path / "scratch"),
        "thread_cap": 3,
    }
    assert document["roots"] == {root.id: str(root.path) for root in config.roots}
    assert "test-token" not in json.dumps(document)


def test_generated_config_is_accepted_by_the_worker_loader(tmp_path, monkeypatch):
    config = make_config(tmp_path, scratch_dir=tmp_path / "scratch")
    path = tmp_path / "local-worker.toml"
    path.write_bytes(build_local_worker_toml(config, worker_config_path=path))
    monkeypatch.setenv(TOKEN_ENV_VAR, "test-token")

    worker = load_worker_config(path)
    assert worker.worker_id == "local"
    assert worker.controller_url == "http://127.0.0.1:8765"
    assert worker.token == "test-token"
    assert worker.thread_cap == 2
    assert {root_id: Path(path).resolve() for root_id, path in worker.roots.items()} == {
        root.id: root.path.resolve() for root in config.roots
    }


def test_worker_command_matches_the_frozen_service_shape(tmp_path):
    path = tmp_path / "local-worker.toml"
    command = worker_command(path)
    assert command[0] == sys.executable  # unresolved: a venv symlink must stay in venv
    assert command[1:] == default_worker_command(path)[1:]
    assert service_command(path, "uninstall-service")[-2:] == ("uninstall-service", "--json")
