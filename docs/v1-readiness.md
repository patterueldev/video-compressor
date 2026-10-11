# V1 readiness check (issue #13)

Final check against the plan.md "V1 is ready" sentence. Status values:
**done** (evidence committed), **operator-gated** (the code is ready; evidence needs the
operator's real environment), **proposed** (deferred; follow-up drafts are recorded in the #13
pull request until filed).

| plan.md clause | Status | Evidence |
| --- | --- | --- |
| Select videos (drop or dialogs) and convert to a chosen destination | done; operator evidence pending | Enqueue API and job model (#39: `POST /api/v1/jobs`, `controller/enqueue.py`), destination placement and optional verified delete (#40: `core/place.py`, `core/records.py`, `worker/destination.py`), and the GUI convert flow (#41, PR #49: `gui/convert_panel.py`, `convert_service.py`); tests `tests/test_controller_enqueue.py`, `tests/test_place.py`, `tests/worker/test_pipeline_destination*.py`, `tests/gui/test_convert_*.py`. The real run is the #44 acceptance milestone below. |
| Start from the GUI and watch the run | done; operator evidence pending | The Convert tab enqueues through the #39 API and switches to the Monitor tab (#41); the job monitor (issue #9) shows progress, stop/resume, and the run summary. The operator run is the #44 acceptance milestone below. |
| Use one or more workers safely | done, operator evidence pending | Worker agent (#6), controller leases and transactional claims (#7), container node (#10); `scripts/two_node_check.py`; the real homeserver two-node run is operator-gated ([deploy/homeserver/README.md](../deploy/homeserver/README.md)). |
| Replace or delete validated sources only after a verified conversion | done, acceptance run operator-gated | Verified replacement protocol (#5), destination placement with delete-after-validation (#40), worker pipeline (#6), fault-injection matrix. The #12 pilot ceremony is superseded; the one remaining real-clip checkpoint is the #44 acceptance run (delete off, then delete on a copy). |
| Re-run without redoing completed clips or losing a source | done for tests; real run operator-gated | `scripts/rerun_matrix.py` scenarios pass synthetically (`tests/test_rerun_matrix_slow.py`); the real-folder matrix run is the #13 operator checkpoint below. |

## Operator milestones (in order)

1. **#44 acceptance run**: convert a small real folder with delete off, then a copy with delete
   on, both nodes engaged; record sanitized evidence. This supersedes the #12 pilot gate, whose
   settings decision is already recorded in [pilot/settings-review.md](pilot/settings-review.md).
2. **#13 real-folder matrix**: run `scripts/rerun_matrix.py` against the mounted archive (small
   clip, `$MATRIX_ROOT` outside it), fill the results table in [pilot-notes.md](pilot-notes.md),
   and keep the sanitized report at `docs/pilot/reports/rerun-matrix.json`.
3. **#10 homeserver evidence**: the real two-node run with responsiveness notes
   ([deploy/homeserver/README.md](../deploy/homeserver/README.md)).
4. **Full archive run**: process the archive and the next import folder; confirm the rerun
   behavior on the real footage. This is the V1 sign-off.

## Deferrals (proposed follow-ups; drafts in the #13 pull request)

- **F1 — GUI queue-start slice**: resolved in code — the enqueue API (#39) and the GUI convert
  flow (#41, merged) start runs from the app; confirm it in the #44 acceptance run.
- **F2 — Retry/requeue failed and review clips**: no operator retry path outside
  `scripts/pilot_run.py enqueue --requeue` (re-enqueue reports `previously_failed` for failed
  records), and `core/scan.py::_decide` keeps review records in review until the manifest is
  reset.
- **F3 — Per-vendor capture-time handling**: GoPro local-with-Z vs DJI true UTC
  ([pilot-notes.md](pilot-notes.md)).
- **F4 — Camera-clock-reset ("wrong dates") detection**: candidate heuristic noted in
  [pilot-notes.md](pilot-notes.md).

Recorded, not filed: the committed #11 pilot reports and notes contain the operator's
share/device names (consider sanitizing when the archive is next touched); the +8 h MOTORCYCLE
filename prefixes remain an operator data decision.
