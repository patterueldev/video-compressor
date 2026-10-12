"""Per-node entries for the remote worker nodes (issue #54).

One entry describes a worker host the operator manages from the Nodes tab: the
SSH target, the controller URL reachable from that host, the controller root id
to path mappings, and the worker's scratch dir and thread cap. Entries persist in
the per-user config as a ``[[nodes]]`` array of tables; the worker's own
``worker.toml`` is rendered and pushed by ``videocompressor.node`` (issue #53),
never here.

Pure stdlib — ``core`` may not import ``worker`` (docs/architecture.md), so the
id pattern and the thread-cap default deliberately mirror ``worker/config.py``;
``tests/gui/test_nodes_config.py`` asserts the parity.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from urllib.parse import urlsplit

__all__ = [
    "DEFAULT_THREAD_CAP",
    "NODE_ID_PATTERN",
    "NodeEntry",
    "add_node",
    "nodes_from_toml",
    "nodes_to_toml",
    "remove_node",
    "update_node",
    "validate_node",
]

#: Worker ids match this pattern in the controller contract; mirrors
#: ``videocompressor.worker.config.WORKER_ID_PATTERN`` (core may not import worker).
NODE_ID_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,64}")

#: Mirrors ``videocompressor.worker.config.DEFAULT_THREAD_CAP``.
DEFAULT_THREAD_CAP = 2


@dataclass(frozen=True)
class NodeEntry:
    """One configured worker node (the ``[[nodes]]`` config entry)."""

    id: str
    name: str = ""
    host: str = ""
    user: str = ""
    port: int | None = None
    identity: str = ""
    strict_host_keys: bool = False
    controller_url: str = ""
    roots: Mapping[str, str] = field(default_factory=dict)
    scratch_dir: str = ""
    thread_cap: int = DEFAULT_THREAD_CAP
    install_dir: str = ""

    @property
    def display_name(self) -> str:
        """The label shown for this node (the id when no name is set)."""
        return self.name or self.id


def validate_node(
    entry: NodeEntry,
    existing: Sequence[NodeEntry] = (),
    *,
    original_id: str | None = None,
) -> list[str]:
    """Human-readable problems with ``entry`` (empty when valid).

    ``existing`` supplies the sibling entries for the duplicate-id check;
    ``original_id`` exempts the entry being edited from that check.
    """
    problems: list[str] = []
    if not NODE_ID_PATTERN.fullmatch(entry.id):
        problems.append("node id must be 1-64 letters, digits, dot, underscore or dash")
    elif any(node.id == entry.id and node.id != original_id for node in existing):
        problems.append(f"node id {entry.id!r} is already registered")
    if not entry.host.strip():
        problems.append("host is required")
    elif any(character.isspace() for character in entry.host):
        problems.append("host must not contain spaces")
    if entry.port is not None and not 1 <= entry.port <= 65535:
        problems.append("port must be between 1 and 65535")
    if entry.thread_cap < 1:
        problems.append("thread cap must be a positive integer")
    if not entry.scratch_dir.strip():
        problems.append("scratch dir is required")
    if not entry.roots:
        problems.append("map at least one root id to a path on that host")
    for root_id, path in entry.roots.items():
        if not NODE_ID_PATTERN.fullmatch(root_id):
            problems.append(f"root id {root_id!r} must match [A-Za-z0-9_.-]{{1,64}}")
        if not path.strip():
            problems.append(f"root {root_id!r} needs a path on that host")
    if entry.controller_url:
        problems.extend(_controller_url_problems(entry.controller_url))
    return problems


def add_node(nodes: list[NodeEntry], entry: NodeEntry) -> NodeEntry:
    """Append a validated node, rejecting duplicate ids."""
    problems = validate_node(entry, nodes)
    if problems:
        raise ValueError("; ".join(problems))
    nodes.append(entry)
    return entry


def update_node(nodes: list[NodeEntry], original_id: str, entry: NodeEntry) -> NodeEntry:
    """Replace the node with ``original_id``, preserving its position."""
    for index, node in enumerate(nodes):
        if node.id == original_id:
            problems = validate_node(entry, nodes, original_id=original_id)
            if problems:
                raise ValueError("; ".join(problems))
            nodes[index] = entry
            return entry
    raise ValueError(f"unknown node {original_id!r}")


def remove_node(nodes: list[NodeEntry], node_id: str) -> NodeEntry:
    """Remove and return the node with ``node_id``."""
    for index, node in enumerate(nodes):
        if node.id == node_id:
            return nodes.pop(index)
    raise ValueError(f"unknown node {node_id!r}")


def nodes_to_toml(nodes: Sequence[NodeEntry]) -> list[dict[str, object]]:
    """Render the ``[[nodes]]`` array; empty optional values are omitted."""
    return [_node_to_toml(node) for node in nodes]


def nodes_from_toml(value: object) -> list[NodeEntry]:
    """Parse the ``[[nodes]]`` array defensively; malformed entries are dropped."""
    if not isinstance(value, list):
        return []
    nodes: list[NodeEntry] = []
    seen: set[str] = set()
    for item in value:
        node = _node_from_toml(item)
        if node is None or node.id in seen:
            continue
        seen.add(node.id)
        nodes.append(node)
    return nodes


def _controller_url_problems(value: str) -> list[str]:
    split = urlsplit(value.strip())
    if split.scheme not in ("http", "https") or not split.netloc:
        return [f"controller URL {value!r} must be an http(s) URL"]
    if split.path not in ("", "/") or split.query or split.fragment:
        return [f"controller URL {value!r} must not carry a path"]
    return []


def _node_to_toml(node: NodeEntry) -> dict[str, object]:
    document: dict[str, object] = {"id": node.id, "host": node.host}
    if node.name:
        document["name"] = node.name
    if node.user:
        document["user"] = node.user
    if node.port is not None:
        document["port"] = node.port
    if node.identity:
        document["identity"] = node.identity
    if node.strict_host_keys:
        document["strict_host_keys"] = True
    if node.controller_url:
        document["controller_url"] = node.controller_url
    document["scratch_dir"] = node.scratch_dir
    document["thread_cap"] = node.thread_cap
    if node.install_dir:
        document["install_dir"] = node.install_dir
    if node.roots:
        document["roots"] = dict(node.roots)
    return document


def _node_from_toml(item: object) -> NodeEntry | None:
    if not isinstance(item, Mapping):
        return None
    node_id = _text(item.get("id"))
    host = _text(item.get("host"))
    if not node_id or not host:
        return None
    return NodeEntry(
        id=node_id,
        name=_text(item.get("name")),
        host=host,
        user=_text(item.get("user")),
        port=_port(item.get("port")),
        identity=_text(item.get("identity")),
        strict_host_keys=item.get("strict_host_keys") is True,
        controller_url=_text(item.get("controller_url")),
        roots=_roots_map(item.get("roots")),
        scratch_dir=_text(item.get("scratch_dir")),
        thread_cap=_thread_cap(item.get("thread_cap")),
        install_dir=_text(item.get("install_dir")),
    )


def _roots_map(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(root_id): str(path)
        for root_id, path in value.items()
        if isinstance(root_id, str) and isinstance(path, str) and path.strip()
    }


def _thread_cap(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return DEFAULT_THREAD_CAP
    return value


def _port(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        return None
    return value


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""
