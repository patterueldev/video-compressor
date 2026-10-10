# V1 readiness check (issue #13)

Final check against the plan.md "V1 is ready" sentence (`plan.md:140-142`). Status values:
**done** (evidence committed), **operator-gated** (the code is ready; evidence needs the
operator's real environment), **proposed** (deferred; follow-up drafts are recorded in the #13
pull request until filed).

| plan.md clause | Status | Evidence |
| --- | --- | --- |
| Drag in SMB footage folders | done | GUI drop zone and folder picker (`src/videocompressor/gui/`) over the read-only scan (issue #3); real-folder dry runs on the mounted share recorded in [pilot-notes.md](pilot-notes.md) (issue #11). |
| Review and configure the queue | done for review/edit | Queue and review tables with per-clip bitrate and capture-time edits (`gui/state.py`, `gui/clip_tables.py`), plus the job monitor (issue #9). Starting a processing run from the app is **proposed** (see F1); the operator seeds the queue with tooling today. |
| Use one or more workers safely | done, operator evidence pending | Worker agent (#6), controller leases and transactional claims (#7), container node (#10); `scripts/two_node_check.py`; the real homeserver two-node run is operator-gated ([deploy/homeserver/README.md](../deploy/homeserver/README.md)). |
| Replace validated sources | done, first real replacement operator-gated | Verified replacement protocol (#5), worker pipeline (#6), fault-injection matrix; the watched first replacement (#12, [pilot/first-replacement.md](pilot/first-replacement.md)) needs the operator's settings review (CP-1, `docs/pilot/settings-review.md`). |
| Resume processing across later imports without redoing completed clips | done for tests; real run operator-gated | `scripts/rerun_matrix.py` scenarios pass synthetically (`tests/test_rerun_matrix_slow.py`); the real-folder matrix run is the #13 operator checkpoint below. |

## Operator milestones (in order)

1. **#12 pilot gate**: record the settings review (CP-1) in `docs/pilot/settings-review.md`, run
   the watched first replacement ([pilot/first-replacement.md](pilot/first-replacement.md)), and
   fill its evidence table.
2. **#13 real-folder matrix**: run `scripts/rerun_matrix.py` against the mounted archive (small
   clip, `$MATRIX_ROOT` outside it), fill the results table in [pilot-notes.md](pilot-notes.md),
   and keep the sanitized report at `docs/pilot/reports/rerun-matrix.json`.
3. **#10 homeserver evidence**: the real two-node run with responsiveness notes
   ([deploy/homeserver/README.md](../deploy/homeserver/README.md)).
4. **Full archive run (plan.md step 7)**: process the archive and the next import folder;
   confirm the rerun behavior on the real footage. This is the V1 sign-off.

## Deferrals (proposed follow-ups; drafts in the #13 pull request)

- **F1 — GUI queue-start slice**: write the reviewed/edited queue to the durable manifest and
  start a processing run from the app (`gui/state.py`: "the durable queue write belongs to the
  processing slice").
- **F2 — Retry/requeue failed and review clips**: no operator retry path outside
  `scripts/pilot_run.py enqueue --requeue`, and `core/scan.py::_decide` keeps review records in
  review until the manifest is reset.
- **F3 — Per-vendor capture-time handling**: GoPro local-with-Z vs DJI true UTC
  ([pilot-notes.md](pilot-notes.md)).
- **F4 — Camera-clock-reset ("wrong dates") detection**: candidate heuristic noted in
  [pilot-notes.md](pilot-notes.md).

Recorded, not filed: the committed #11 pilot reports and notes contain the operator's
share/device names (consider sanitizing when the archive is next touched); the +8 h MOTORCYCLE
filename prefixes remain an operator data decision.
