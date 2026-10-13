"""Machine-aware registry duplicate decision (issue #80).

Pure Python — runs without the optional PySide6 extra, like the other
supervisor suites. ``find_duplicate`` is covered directly; the supervisor
wiring tests prove the app-level behavior (a remote node with identical roots
must not block the local worker).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from gui.local_worker_support import (
    FakeProcessTable,
    FakeRegistry,
    FakeServiceRunner,
    FakeSpawner,
    make_config,
    roots_payload,
    worker_entry,
)
from gui.monitor_support import make_worker
from videocompressor.core.config import AppConfig
from videocompressor.gui.local_worker import (
    STATE_ALREADY_RUNNING,
    STATE_STARTED_CHILD,
    LocalWorkerSupervisor,
)
from videocompressor.gui.local_worker_duplicate import find_duplicate

LOCAL_ROOTS = {"archive": "/Volumes/archive"}
LOCAL_MACHINE = "workstation-a"
STOPPED_AT = datetime(2026, 10, 11, 9, 0, 0, tzinfo=UTC)


# -- the decision ----------------------------------------------------------------


def test_a_remote_identity_with_identical_roots_is_not_a_duplicate():
    worker = make_worker("macbook", roots=LOCAL_ROOTS, machine_id="remote-host")

    found = find_duplicate(
        (worker,),
        worker_id="local",
        roots=LOCAL_ROOTS,
        local_machine=LOCAL_MACHINE,
        stopped_at=None,
    )

    assert found is None


def test_the_same_machine_identity_under_another_id_is_a_duplicate():
    worker = make_worker("service-worker", roots={}, machine_id=LOCAL_MACHINE)

    found = find_duplicate(
        (worker,),
        worker_id="local",
        roots=LOCAL_ROOTS,
        local_machine=LOCAL_MACHINE,
        stopped_at=None,
    )

    assert found is worker


def test_the_worker_id_match_defers_across_machines():
    worker = make_worker("local", roots={}, machine_id="remote-host")

    found = find_duplicate(
        (worker,),
        worker_id="local",
        roots={},
        local_machine=LOCAL_MACHINE,
        stopped_at=None,
    )

    assert found is worker


def test_a_missing_identity_keeps_the_root_mapping_fallback():
    legacy = make_worker("node-2", roots=LOCAL_ROOTS)

    assert (
        find_duplicate(
            (legacy,),
            worker_id="local",
            roots=LOCAL_ROOTS,
            local_machine=LOCAL_MACHINE,
            stopped_at=None,
        )
        is legacy
    )

    other = make_worker("node-2", roots={"archive": "/srv/archive"})
    assert (
        find_duplicate(
            (other,),
            worker_id="local",
            roots=LOCAL_ROOTS,
            local_machine=LOCAL_MACHINE,
            stopped_at=None,
        )
        is None
    )


def test_a_missing_local_identity_keeps_the_root_mapping_fallback():
    worker = make_worker("node-2", roots=LOCAL_ROOTS, machine_id="remote-host")

    found = find_duplicate(
        (worker,),
        worker_id="local",
        roots=LOCAL_ROOTS,
        local_machine=None,
        stopped_at=None,
    )

    assert found is worker


def test_offline_registrations_never_block():
    worker = make_worker("node-2", online=False, roots=LOCAL_ROOTS, machine_id=LOCAL_MACHINE)

    found = find_duplicate(
        (worker,),
        worker_id="local",
        roots=LOCAL_ROOTS,
        local_machine=LOCAL_MACHINE,
        stopped_at=None,
    )

    assert found is None


def test_the_stop_marker_skips_dead_rows_for_every_match_kind():
    identity_match = make_worker(
        "node-2", roots={}, machine_id=LOCAL_MACHINE, last_seen=STOPPED_AT - timedelta(seconds=30)
    )
    legacy_match = make_worker(
        "node-3", roots=LOCAL_ROOTS, last_seen=STOPPED_AT - timedelta(seconds=30)
    )

    found = find_duplicate(
        (identity_match, legacy_match),
        worker_id="local",
        roots=LOCAL_ROOTS,
        local_machine=LOCAL_MACHINE,
        stopped_at=STOPPED_AT,
    )

    assert found is None


def test_a_row_newer_than_the_stop_marker_still_blocks():
    fresh = make_worker(
        "node-2",
        roots=LOCAL_ROOTS,
        machine_id=LOCAL_MACHINE,
        last_seen=STOPPED_AT + timedelta(seconds=5),
    )

    found = find_duplicate(
        (fresh,),
        worker_id="local",
        roots=LOCAL_ROOTS,
        local_machine=LOCAL_MACHINE,
        stopped_at=STOPPED_AT,
    )

    assert found is fresh


# -- the supervisor wiring -------------------------------------------------------


def supervisor_for(
    tmp_path: Path,
    config: AppConfig,
    *,
    registry: FakeRegistry | None = None,
) -> tuple[LocalWorkerSupervisor, FakeSpawner]:
    """A supervisor with recording fakes and a deterministic machine identity."""
    spawner = FakeSpawner()
    registry = registry if registry is not None else FakeRegistry()
    supervisor = LocalWorkerSupervisor(
        config,
        tmp_path / "config.toml",
        client=registry,
        spawn=spawner,
        run_command=FakeServiceRunner(),
        processes=FakeProcessTable(),
        machine_id=LOCAL_MACHINE,
    )
    return supervisor, spawner


def test_supervisor_starts_when_a_remote_matches_the_local_roots(tmp_path):
    config = make_config(tmp_path)
    registry = FakeRegistry(
        [worker_entry("macbook", roots=roots_payload(config), machine_id="remote-host")]
    )

    supervisor, spawner = supervisor_for(tmp_path, config, registry=registry)

    outcome = supervisor.ensure_started()

    assert outcome.state == STATE_STARTED_CHILD
    assert len(spawner.calls) == 1


def test_supervisor_defers_to_a_registration_from_this_machine(tmp_path):
    config = make_config(tmp_path)
    registry = FakeRegistry([worker_entry("service-worker", roots={}, machine_id=LOCAL_MACHINE)])

    supervisor, spawner = supervisor_for(tmp_path, config, registry=registry)

    outcome = supervisor.ensure_started()

    assert outcome.state == STATE_ALREADY_RUNNING
    assert "service-worker" in outcome.detail
    assert spawner.calls == []
