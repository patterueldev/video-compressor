"""Per-node config entries: schema, validation, and round-trips (issue #54)."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from gui.nodes_support import ARCHIVE, HOST, SCRATCH, make_entry
from videocompressor.core.config import AppConfig, load_config, save_config
from videocompressor.core.nodes import (
    DEFAULT_THREAD_CAP,
    NODE_ID_PATTERN,
    NodeEntry,
    add_node,
    nodes_from_toml,
    nodes_to_toml,
    remove_node,
    update_node,
    validate_node,
)
from videocompressor.worker.config import DEFAULT_THREAD_CAP as WORKER_THREAD_CAP
from videocompressor.worker.config import WORKER_ID_PATTERN


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    return tmp_path / "config.toml"


def test_one_node_round_trips(config_path):
    config = AppConfig()
    add_node(config.nodes, make_entry())
    save_config(config, config_path)

    loaded = load_config(config_path)
    assert loaded.nodes == [make_entry()]
    assert "[[nodes]]" in config_path.read_text(encoding="utf-8")


def test_two_nodes_with_nested_roots_round_trip(config_path):
    config = AppConfig()
    add_node(config.nodes, make_entry())
    add_node(
        config.nodes,
        make_entry(
            id="node-b",
            name="",
            host="node.example.com",
            user="",
            port=None,
            identity="",
            strict_host_keys=True,
            roots={"archive": ARCHIVE, "compressed": "/srv/compressed"},
            scratch_dir="/srv/scratch",
            thread_cap=4,
            install_dir="~/.videocompressor",
        ),
    )
    save_config(config, config_path)

    loaded = load_config(config_path)
    assert [node.id for node in loaded.nodes] == ["node-a", "node-b"]
    assert loaded.nodes[1].roots == {"archive": ARCHIVE, "compressed": "/srv/compressed"}
    assert loaded.nodes[1].strict_host_keys is True
    assert loaded.nodes[1].thread_cap == 4
    assert loaded.nodes[1].install_dir == "~/.videocompressor"


def test_empty_nodes_section_absent_from_written_config(config_path):
    path = save_config(AppConfig(), config_path)
    assert "nodes" not in path.read_text(encoding="utf-8")


def test_optional_fields_are_omitted_on_write(config_path):
    config = AppConfig()
    add_node(
        config.nodes,
        NodeEntry(id="bare", host=HOST, scratch_dir=SCRATCH, roots={"archive": ARCHIVE}),
    )
    save_config(config, config_path)
    document = tomllib.loads(config_path.read_text(encoding="utf-8"))

    assert set(document["nodes"][0]) == {"id", "host", "scratch_dir", "thread_cap", "roots"}
    assert load_config(config_path).nodes[0] == config.nodes[0]


def test_hand_written_nodes_table_round_trips(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        "[[nodes]]\n"
        'id = "node-a"\n'
        f'host = "{HOST}"\n'
        f'scratch_dir = "{SCRATCH}"\n'
        "\n"
        "[nodes.roots]\n"
        f'archive = "{ARCHIVE}"\n',
        encoding="utf-8",
    )

    config = load_config(path)
    assert config.nodes == [
        NodeEntry(id="node-a", host=HOST, scratch_dir=SCRATCH, roots={"archive": ARCHIVE})
    ]


def test_malformed_and_duplicate_entries_are_dropped():
    entries = nodes_from_toml(
        [
            "not a table",
            {"id": "", "host": HOST},
            {"id": "no-host"},
            {"id": "dup", "host": HOST},
            {"id": "dup", "host": "node.example.com"},
            {"id": "ok", "host": HOST, "port": "ssh", "thread_cap": 0, "roots": {"a": "  "}},
        ]
    )
    assert [node.id for node in entries] == ["dup", "ok"]
    assert entries[-1].port is None
    assert entries[-1].thread_cap == DEFAULT_THREAD_CAP
    assert entries[-1].roots == {}


def test_nodes_from_toml_tolerates_non_list():
    assert nodes_from_toml(None) == []
    assert nodes_from_toml({"id": "single"}) == []
    assert nodes_to_toml([]) == []


def test_add_node_rejects_duplicate_ids_and_rolls_back_nothing():
    nodes = [make_entry()]
    with pytest.raises(ValueError, match="already registered"):
        add_node(nodes, make_entry(name="other"))
    assert nodes == [make_entry()]


def test_update_node_replaces_in_place_and_can_rename():
    nodes = [make_entry(id="node-a"), make_entry(id="node-b")]
    updated = update_node(nodes, "node-a", make_entry(id="node-c", name="Renamed"))
    assert updated.id == "node-c"
    assert [node.id for node in nodes] == ["node-c", "node-b"]
    with pytest.raises(ValueError, match="already registered"):
        update_node(nodes, "node-b", make_entry(id="node-c"))
    with pytest.raises(ValueError, match="unknown node"):
        update_node(nodes, "ghost", make_entry(id="ghost"))


def test_remove_node_errors_on_unknown_id():
    nodes = [make_entry()]
    assert remove_node(nodes, "node-a").id == "node-a"
    assert nodes == []
    with pytest.raises(ValueError, match="unknown node"):
        remove_node(nodes, "node-a")


def test_validate_node_accepts_the_factory_entry():
    assert validate_node(make_entry()) == []


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"id": "bad id"}, "node id must be"),
        ({"host": ""}, "host is required"),
        ({"host": "two words"}, "must not contain spaces"),
        ({"port": 70000}, "port must be"),
        ({"thread_cap": 0}, "thread cap"),
        ({"scratch_dir": "  "}, "scratch dir is required"),
        ({"roots": {}}, "map at least one root"),
        ({"roots": {"bad id": ARCHIVE}}, "root id"),
        ({"roots": {"archive": "  "}}, "needs a path"),
        ({"controller_url": "controller.example.com"}, "http(s) URL"),
        ({"controller_url": "http://host:8765/jobs"}, "must not carry a path"),
    ],
)
def test_validate_node_reports_each_problem(overrides, fragment):
    entry = make_entry(**overrides)
    problems = validate_node(entry)
    assert any(fragment in problem for problem in problems), problems


def test_validate_node_checks_duplicates_except_the_edited_id():
    nodes = [make_entry(id="node-a")]
    duplicate = validate_node(make_entry(id="node-a"), nodes)
    assert any("already registered" in problem for problem in duplicate)
    assert validate_node(make_entry(id="node-a"), nodes, original_id="node-a") == []


def test_id_pattern_and_thread_cap_match_the_worker_contract():
    assert NODE_ID_PATTERN.pattern == WORKER_ID_PATTERN.pattern
    assert DEFAULT_THREAD_CAP == WORKER_THREAD_CAP
