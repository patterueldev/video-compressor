"""Monitor payload parsing, formatting, and the snapshot/event tail (issue #9)."""

from __future__ import annotations

from datetime import timedelta

from gui.monitor_support import (
    ACTIVE_PATH,
    NOW,
    FakeMonitorClient,
    event_payload,
    events_payload,
    make_progress,
    make_summary,
    summary_payload,
    worker_payload,
    workers_payload,
)
from videocompressor.gui.monitor_service import (
    MonitorSession,
    format_eta,
    format_last_seen,
    format_progress,
    format_roots,
    format_speed,
    format_summary,
    parse_events,
    parse_summary,
    parse_workers,
)


def test_parse_workers_reads_state_clip_roots_and_progress():
    workers = parse_workers(workers_payload(worker_payload()))
    assert len(workers) == 1
    worker = workers[0]
    assert worker.worker_id == "homeserver"
    assert worker.name == "homeserver"
    assert worker.online is True
    assert worker.last_seen == NOW - timedelta(seconds=12)
    assert worker.roots == {"archive": "/srv/archive"}
    assert worker.machine_id == "homeserver"
    assert worker.stop_requested is False
    assert worker.stop_mode is None
    assert worker.active_root_id == "archive"
    assert worker.active_path == ACTIVE_PATH
    assert worker.progress_at == NOW
    assert worker.progress is not None
    assert worker.progress.phase == "encoding"
    assert worker.progress.pass_number == 2
    assert worker.progress.percent == 41.2
    assert worker.progress.fps == 38.5
    assert worker.progress.speed == "0.74x"
    assert worker.progress.eta_seconds == 1234.0


def test_parse_workers_tolerates_missing_and_malformed_fields():
    payload = workers_payload(
        {"worker_id": "a"},
        {"no_worker_id": True},
        "not-a-mapping",
        worker_payload("b", active=None, progress=None, last_seen="not-a-time", roots={"x": 1}),
    )
    workers = parse_workers(payload)
    assert [worker.worker_id for worker in workers] == ["a", "b"]
    first, second = workers
    assert first.online is False
    assert first.progress is None
    assert first.active_path is None
    assert first.last_seen is None
    assert first.machine_id is None
    assert second.roots == {}
    assert second.last_seen is None
    assert parse_workers({"workers": "nope"}) == ()


def test_parse_workers_reads_the_stop_mode():
    workers = parse_workers(
        workers_payload(
            worker_payload("a", stop_requested=True, stop_mode="drain"),
            worker_payload("b", stop_requested=True, stop_mode=7),
            worker_payload("c", stop_requested=False, stop_mode="cancel"),
        )
    )
    assert workers[0].stop_mode == "drain"
    assert workers[1].stop_mode is None  # a mistyped value degrades, never raises
    assert workers[2].stop_mode == "cancel"


def test_parse_events_reads_sequence_and_next():
    events, next_seq = parse_events(events_payload([event_payload(1), event_payload(2)]))
    assert [event.seq for event in events] == [1, 2]
    assert next_seq == 3
    assert events[0].message == "claimed GS010001.MP4"
    assert events[0].relative_path == ACTIVE_PATH
    assert events[0].at == NOW


def test_parse_events_defaults_next_and_skips_malformed():
    events, next_seq = parse_events(
        {"events": [{"message": "no seq"}, {"seq": 7}, event_payload(9)]}
    )
    assert [event.seq for event in events] == [9]
    assert next_seq == 10
    assert parse_events({}) == ((), 0)


def test_parse_summary_defaults():
    counts = parse_summary(summary_payload(completed=4))
    assert (counts.completed, counts.skipped, counts.failed, counts.review) == (4, 12, 0, 1)
    assert counts.run_id is None
    empty = parse_summary({})
    assert (empty.completed, empty.skipped, empty.failed, empty.review) == (0, 0, 0, 0)


def test_format_eta():
    assert format_eta(None) == "-"
    assert format_eta(42) == "42s"
    assert format_eta(252) == "4m 12s"
    assert format_eta(3900) == "1h 05m"
    assert format_eta(-3) == "0s"


def test_format_speed_and_progress():
    assert format_speed(make_progress()) == "38.5 fps · 0.74x"
    assert format_speed(make_progress(fps=None)) == "0.74x"
    assert format_speed(make_progress(fps=None, speed=None)) == "-"
    assert format_speed(None) == "-"
    assert format_progress(make_progress()) == "encoding · pass 2 · 41.2%"
    assert format_progress(make_progress(phase=None, pass_number=None, percent=None)) == "-"
    assert format_progress(None) == "-"


def test_format_last_seen():
    assert format_last_seen(None, NOW) == "never"
    assert format_last_seen(NOW - timedelta(seconds=2), NOW) == "just now"
    assert format_last_seen(NOW - timedelta(seconds=12), NOW) == "12s ago"
    assert format_last_seen(NOW - timedelta(minutes=3), NOW) == "3m ago"
    assert format_last_seen(NOW - timedelta(hours=2, minutes=5), NOW) == "2h 05m ago"


def test_format_roots_and_summary():
    assert format_roots({}) == "-"
    assert format_roots({"archive": "/srv/archive"}) == "archive → /srv/archive"
    assert format_roots({"a": "/x", "b": "/y"}) == "a → /x; b → /y"
    assert format_summary(make_summary()) == "completed 3 · skipped 12 · failed 0 · review-needed 1"


def test_session_snapshot_tails_events_and_reuses_the_cursor():
    client = FakeMonitorClient(
        workers=workers_payload(worker_payload()),
        event_pages=[
            events_payload([event_payload(1), event_payload(2)], next_seq=3),
            events_payload([event_payload(3)], next_seq=4),
        ],
        summary=summary_payload(),
    )
    session = MonitorSession(client)

    first = session.snapshot()
    assert client.event_calls == [None]
    assert [event.seq for event in first.events] == [1, 2]
    assert first.summary.completed == 3
    assert first.workers[0].worker_id == "homeserver"

    second = session.snapshot()
    assert client.event_calls == [None, 3]
    assert [event.seq for event in second.events] == [1, 2, 3]


def test_session_bounds_the_event_tail():
    client = FakeMonitorClient(
        event_pages=[
            events_payload([event_payload(1), event_payload(2)], next_seq=3),
            events_payload([event_payload(3)], next_seq=4),
        ]
    )
    session = MonitorSession(client, max_events=2)
    session.snapshot()
    assert [event.seq for event in session.snapshot().events] == [2, 3]


def test_session_events_for_filters_by_clip_and_worker():
    client = FakeMonitorClient(
        event_pages=[
            events_payload(
                [
                    event_payload(1),
                    event_payload(2, worker_id="other", relative_path="other.MP4"),
                ]
            )
        ]
    )
    session = MonitorSession(client)
    session.snapshot()
    assert [event.seq for event in session.events_for(relative_path=ACTIVE_PATH)] == [1]
    assert [event.seq for event in session.events_for(worker_id="other")] == [2]
    assert [
        event.seq
        for event in session.events_for(worker_id="homeserver", relative_path=ACTIVE_PATH)
    ] == [1]


def test_session_delegates_stop_and_resume():
    client = FakeMonitorClient()
    session = MonitorSession(client)
    session.stop_worker("homeserver")
    session.stop_worker("homeserver", mode="drain")
    session.resume_worker("homeserver")
    assert client.stops == [("homeserver", "cancel"), ("homeserver", "drain")]
    assert client.resumes == ["homeserver"]
