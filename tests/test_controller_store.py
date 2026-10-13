"""ControllerStore: worker registry, events, runs, and restart durability."""

from __future__ import annotations

import sqlite3

import pytest

from videocompressor.controller.store import ControllerStore
from videocompressor.core.models import StopMode


def test_worker_registry_round_trip(store, clock):
    row = store.upsert_worker(
        "homeserver",
        name="Linux homeserver",
        roots={"archive": "/srv/archive"},
        scratch_dir="/srv/scratch",
        thread_cap=2,
        version="0.1.0",
    )
    assert row.worker_id == "homeserver"
    assert row.name == "Linux homeserver"
    assert row.roots == {"archive": "/srv/archive"}
    assert row.scratch_dir == "/srv/scratch"
    assert row.version == "0.1.0"
    assert row.stop_requested is False
    assert row.last_seen == clock()
    assert store.get_worker("homeserver") == row
    assert store.get_worker("gap") is None
    assert [worker.worker_id for worker in store.list_workers()] == ["homeserver"]


def test_reregister_refreshes_and_clears_stop_flag(store, clock):
    store.upsert_worker("a", name="old")
    store.set_stop_requested("a", True, mode=StopMode.DRAIN)
    clock.advance(10)
    row = store.upsert_worker("a", name="new", version="0.2.0")
    assert row.name == "new"
    assert row.stop_requested is False
    assert row.stop_mode is None
    assert row.last_seen == clock()


def test_stop_mode_round_trip_and_clear(store):
    store.upsert_worker("a")
    row = store.set_stop_requested("a", True, mode=StopMode.DRAIN)
    assert row is not None
    assert row.stop_requested is True
    assert row.stop_mode is StopMode.DRAIN

    row = store.set_stop_requested("a", True, mode=StopMode.CANCEL)
    assert row is not None and row.stop_mode is StopMode.CANCEL

    row = store.set_stop_requested("a", False)
    assert row is not None
    assert row.stop_requested is False
    assert row.stop_mode is None

    # a stop without an explicit mode defaults to cancel
    row = store.set_stop_requested("a", True)
    assert row is not None and row.stop_mode is StopMode.CANCEL


def test_legacy_database_migrates_the_stop_mode_column(tmp_path, clock):
    path = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE workers (
            worker_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            roots_json TEXT NOT NULL,
            scratch_dir TEXT,
            thread_cap INTEGER NOT NULL,
            version TEXT,
            last_seen TEXT,
            stop_requested INTEGER NOT NULL DEFAULT 0,
            registered_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO workers (worker_id, name, roots_json, scratch_dir, thread_cap, version,"
        " last_seen, stop_requested, registered_at)"
        " VALUES ('legacy', 'legacy', '{}', NULL, 2, NULL, NULL, 1, ?)",
        (clock().isoformat(),),
    )
    conn.commit()
    conn.close()

    store = ControllerStore.open(path, clock=clock)
    try:
        row = store.get_worker("legacy")
        assert row is not None
        assert row.stop_requested is True
        assert row.stop_mode is None  # pre-#74 rows normalize to cancel on read

        updated = store.set_stop_requested("legacy", True, mode=StopMode.DRAIN)
        assert updated is not None and updated.stop_mode is StopMode.DRAIN
    finally:
        store.close()


def test_touch_and_stop_unknown_worker(store):
    assert store.touch_worker("ghost") is None
    assert store.set_stop_requested("ghost", True) is None


def test_events_sequence_after_filter_and_fields(store):
    first = store.append_event("info", "one")
    second = store.append_event("warning", "two", worker_id="a", run_id="r1")
    third = store.append_event("error", "three", root_id="archive", relative_path="x.mp4")
    assert [first.seq, second.seq, third.seq] == [1, 2, 3]
    assert [event.message for event in store.list_events()] == ["one", "two", "three"]
    assert [event.message for event in store.list_events(after=1)] == ["two", "three"]
    assert second.worker_id == "a"
    assert second.run_id == "r1"
    assert third.root_id == "archive"
    assert third.relative_path == "x.mp4"


def test_events_most_recent_bounded(store):
    for index in range(5):
        store.append_event("info", f"event {index}")
    recent = store.list_events(limit=2)
    assert [event.message for event in recent] == ["event 3", "event 4"]


def test_run_lifecycle(store, clock):
    run = store.start_run()
    assert run.ended_at is None
    assert store.active_run() == run
    finished = store.finish_run(run.run_id, completed=3, skipped=1, failed=1, review=2)
    assert finished.ended_at == clock()
    assert (finished.completed, finished.skipped, finished.failed, finished.review) == (3, 1, 1, 2)
    assert store.active_run() is None
    assert store.get_run(run.run_id) == finished
    assert store.get_run("missing") is None


def test_finish_unknown_run_raises(store):
    with pytest.raises(KeyError):
        store.finish_run("nope", completed=0, skipped=0, failed=0, review=0)


def test_state_survives_reopen(tmp_path, clock):
    path = tmp_path / "controller.sqlite"
    first = ControllerStore.open(path, clock=clock)
    first.upsert_worker("homeserver", name="homeserver")
    first.append_event("info", "hello")
    run = first.start_run("run-1")
    first.finish_run(run.run_id, completed=1, skipped=0, failed=0, review=0)
    first.close()

    second = ControllerStore.open(path, clock=clock)
    try:
        assert second.get_worker("homeserver") is not None
        assert [event.message for event in second.list_events()] == ["hello"]
        run_row = second.get_run("run-1")
        assert run_row is not None and run_row.completed == 1
    finally:
        second.close()
