"""Static deployment-artifact checks (issue #10): image, compose, and config templates.

These stay fast and docker-free: the Dockerfile and compose files are validated by
text-level assertions against the deployment contract, and the worker config template is
rendered and loaded through the real ``load_worker_config`` validator.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from videocompressor.worker.config import WorkerConfigError, load_worker_config

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy" / "homeserver"

ARCHIVE_PLACEHOLDER = "/srv/archive"
SCRATCH_PLACEHOLDER = "/var/lib/videocompressor/scratch"


def read_deploy(name: str) -> str:
    return (DEPLOY / name).read_text(encoding="utf-8")


def render_example(tmp_path: Path) -> Path:
    """Render the config template with real temp paths for this machine."""
    archive = tmp_path / "archive"
    archive.mkdir()
    text = read_deploy("worker.example.toml")
    text = text.replace(ARCHIVE_PLACEHOLDER, str(archive))
    text = text.replace(SCRATCH_PLACEHOLDER, str(tmp_path / "scratch"))
    path = tmp_path / "worker.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_worker_example_loads_with_the_token_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("VC_WORKER_TOKEN", "token-placeholder")

    config = load_worker_config(render_example(tmp_path))

    assert config.worker_id == "homeserver"
    assert config.thread_cap == 2  # within the issue's 2-3 cap for the 2-core homeserver
    assert config.token == "token-placeholder"
    assert config.roots == {"archive": (tmp_path / "archive").resolve()}
    assert config.scratch_dir == (tmp_path / "scratch").resolve()


def test_worker_example_requires_the_env_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("VC_WORKER_TOKEN", raising=False)

    with pytest.raises(WorkerConfigError, match="VC_WORKER_TOKEN"):
        load_worker_config(render_example(tmp_path))


def test_worker_example_keeps_the_token_out_of_band():
    text = read_deploy("worker.example.toml")

    assert 'token_env = "VC_WORKER_TOKEN"' in text
    assert not [line for line in text.splitlines() if line.strip().startswith("token =")]


def test_compose_matches_the_deployment_contract():
    text = read_deploy("compose.yaml")

    assert ":/srv/archive:rw" in text
    assert "restart: unless-stopped" in text
    assert "VC_WORKER_TOKEN" in text
    assert ":/config/worker.toml:ro" in text
    assert "stop_grace_period" in text
    scratch_lines = [
        line for line in text.splitlines() if SCRATCH_PLACEHOLDER in line
    ]
    assert len(scratch_lines) == 1
    assert ":ro" not in scratch_lines[0]


def test_dockerfile_matches_the_image_contract():
    text = read_deploy("Dockerfile")

    assert "FROM python:3.12" in text
    assert "ffmpeg" in text
    assert "pip install --no-cache-dir ." in text  # base package only, no gui extra
    assert ".[gui]" not in text
    assert 'ENTRYPOINT ["vc", "worker"]' in text


def test_env_example_has_no_secret_values():
    text = read_deploy(".env.example")

    token_lines = [line for line in text.splitlines() if line.startswith("VC_WORKER_TOKEN=")]
    assert token_lines == ["VC_WORKER_TOKEN="]


def test_deploy_gitignore_covers_local_state():
    entries = set(read_deploy(".gitignore").splitlines())

    assert {".env", "worker.toml"} <= entries
