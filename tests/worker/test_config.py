"""Per-node worker config: loading, validation, and mapping resolution (issue #6)."""

from __future__ import annotations

from pathlib import Path

import pytest

from videocompressor.worker.config import MappingError, WorkerConfigError, load_worker_config

BASE = """
[worker]
id = "{worker_id}"
controller_url = "{controller_url}"
token = "{token}"
scratch_dir = "{scratch}"

[roots]
archive = "{archive}"
"""


def write_config(tmp_path: Path, body: str, name: str = "worker.toml") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def make_config_body(tmp_path: Path, **overrides: object) -> str:
    archive = tmp_path / "archive"
    archive.mkdir(exist_ok=True)
    fields: dict[str, object] = {
        "worker_id": "node-a",
        "controller_url": "http://controller-host:8765",
        "token": "secret-token",
        "scratch": tmp_path / "scratch",
        "archive": archive,
    }
    fields.update(overrides)
    return BASE.format(**fields)


def inject_worker_line(body: str, line: str) -> str:
    return body.replace("[roots]", f"{line}\n\n[roots]")


def test_load_valid_config_reads_fields_and_creates_scratch(tmp_path: Path):
    config = load_worker_config(write_config(tmp_path, make_config_body(tmp_path)))

    assert config.worker_id == "node-a"
    assert config.controller_url == "http://controller-host:8765"
    assert config.token == "secret-token"
    assert config.roots == {"archive": (tmp_path / "archive").resolve()}
    assert config.scratch_dir == (tmp_path / "scratch").resolve()
    assert config.scratch_dir.is_dir()
    assert config.thread_cap == 2
    assert config.poll_seconds == 5.0
    assert config.pilot_root is None
    assert config.pilot is False
    assert config.heartbeat_seconds is None


def test_optional_fields_and_trailing_slash(tmp_path: Path):
    body = make_config_body(tmp_path, controller_url="http://controller-host:8765/")
    body = inject_worker_line(
        body, 'name = "Node A"\nthread_cap = 4\npoll_seconds = 0.5\nheartbeat_seconds = 2.0'
    )
    config = load_worker_config(write_config(tmp_path, body))

    assert config.controller_url == "http://controller-host:8765"
    assert config.name == "Node A"
    assert config.thread_cap == 4
    assert config.poll_seconds == 0.5
    assert config.heartbeat_seconds == 2.0


def test_token_env_is_resolved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VC_TEST_WORKER_TOKEN", "env-token")
    body = make_config_body(tmp_path).replace(
        'token = "secret-token"', 'token_env = "VC_TEST_WORKER_TOKEN"'
    )

    assert load_worker_config(write_config(tmp_path, body)).token == "env-token"


def test_token_env_missing_is_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("VC_TEST_WORKER_TOKEN", raising=False)
    body = make_config_body(tmp_path).replace(
        'token = "secret-token"', 'token_env = "VC_TEST_WORKER_TOKEN"'
    )

    with pytest.raises(WorkerConfigError, match="not set"):
        load_worker_config(write_config(tmp_path, body))


def test_token_and_token_env_together_are_rejected(tmp_path: Path):
    body = make_config_body(tmp_path).replace(
        'token = "secret-token"', 'token = "secret-token"\ntoken_env = "VC_TEST_WORKER_TOKEN"'
    )
    with pytest.raises(WorkerConfigError, match="not both"):
        load_worker_config(write_config(tmp_path, body))


def test_missing_token_is_rejected(tmp_path: Path):
    body = make_config_body(tmp_path).replace('token = "secret-token"\n', "")
    with pytest.raises(WorkerConfigError, match="token"):
        load_worker_config(write_config(tmp_path, body))


@pytest.mark.parametrize(
    ("body_transform", "match"),
    [
        (lambda tmp: lambda body: body.replace('id = "node-a"', 'id = "bad worker!"'), "match"),
        (lambda tmp: lambda body: body.replace("[roots]", ""), "roots"),
        (
            lambda tmp: lambda body: body.replace(
                str(tmp / "archive"), str(tmp / "missing")
            ),
            "roots.archive",
        ),
        (
            lambda tmp: lambda body: body.replace(
                "http://controller-host:8765", "controller-host:8765"
            ),
            "http",
        ),
        (
            lambda tmp: lambda body: body.replace(
                "http://controller-host:8765", "http://host/api/v1"
            ),
            "path",
        ),
        (lambda tmp: lambda body: inject_worker_line(body, "thread_cap = 0"), "thread_cap"),
        (lambda tmp: lambda body: inject_worker_line(body, "poll_seconds = 0"), "poll_seconds"),
    ],
)
def test_invalid_configs_are_rejected(tmp_path: Path, body_transform, match: str):
    body = body_transform(tmp_path)(make_config_body(tmp_path))
    with pytest.raises(WorkerConfigError, match=match):
        load_worker_config(write_config(tmp_path, body))


def test_missing_file_and_invalid_toml(tmp_path: Path):
    with pytest.raises(WorkerConfigError, match="does not exist"):
        load_worker_config(tmp_path / "nope.toml")
    with pytest.raises(WorkerConfigError, match="not valid TOML"):
        load_worker_config(write_config(tmp_path, "[worker\nid = 3"))


def test_missing_worker_section(tmp_path: Path):
    with pytest.raises(WorkerConfigError, match=r"\[worker\]"):
        load_worker_config(write_config(tmp_path, "[roots]\narchive = '/tmp'\n"))


def test_pilot_section_enables_pilot_mode(tmp_path: Path):
    pilot = tmp_path / "pilot"
    body = make_config_body(tmp_path) + f'\n[pilot]\nroot = "{pilot}"\n'
    config = load_worker_config(write_config(tmp_path, body))

    assert config.pilot is True
    assert config.pilot_root == pilot.resolve()
    assert pilot.is_dir()


def test_pilot_section_without_root_is_rejected(tmp_path: Path):
    body = make_config_body(tmp_path) + "\n[pilot]\nother = 1\n"
    with pytest.raises(WorkerConfigError, match=r"\[pilot\]"):
        load_worker_config(write_config(tmp_path, body))


def test_resolve_maps_the_same_relative_path_under_each_root(tmp_path: Path):
    body = make_config_body(tmp_path).replace(
        "[roots]", f'[roots]\nother = "{tmp_path / "other"}"'
    )
    (tmp_path / "other").mkdir()
    config = load_worker_config(write_config(tmp_path, body))

    relative = "2024-06-Ride/GS010001.MP4"
    assert config.resolve("archive", relative) == (
        config.roots["archive"] / "2024-06-Ride" / "GS010001.MP4"
    )
    assert config.resolve("other", relative) == (
        config.roots["other"] / "2024-06-Ride" / "GS010001.MP4"
    )


@pytest.mark.parametrize(
    "relative",
    ["../escape.MP4", "/absolute.MP4", "a/../../escape.MP4", ""],
)
def test_resolve_rejects_unsafe_relative_paths(tmp_path: Path, relative: str):
    config = load_worker_config(write_config(tmp_path, make_config_body(tmp_path)))
    with pytest.raises(MappingError):
        config.resolve("archive", relative)


def test_resolve_normalizes_dot_segments(tmp_path: Path):
    config = load_worker_config(write_config(tmp_path, make_config_body(tmp_path)))
    assert config.resolve("archive", "a/./b.MP4") == config.roots["archive"] / "a" / "b.MP4"


def test_resolve_rejects_unknown_root(tmp_path: Path):
    config = load_worker_config(write_config(tmp_path, make_config_body(tmp_path)))
    with pytest.raises(MappingError, match="not mapped"):
        config.resolve("missing", "clip.MP4")
