# Video Compressor — Agent Rules (canonical, tool-agnostic)

Product spec: [plan.md](plan.md) is the source of truth for behavior, processing flow, safety
rules, and acceptance criteria. Read it before implementing anything in this repository.

## Project

A GUI-driven, resumable GoPro video compressor with optional worker nodes. Python 3.12+ (the
homeserver worker runs 3.12; newer is fine on this Mac). One package, four parts:

- `src/videocompressor/core/` — engine: probing, capture time, classification, manifest,
  encoding, validation, replacement. Never import GUI code from here.
- `src/videocompressor/controller/` — local HTTP controller: durable queue, worker registry,
  leases, dispatch.
- `src/videocompressor/worker/` — worker agent for processing nodes; at most one FFmpeg
  conversion per node.
- `src/videocompressor/gui/` — PySide6 GUI, a thin client that also hosts the controller.

The processing engine must stay independent of the GUI (plan.md requirement). See
`docs/architecture.md` for module boundaries and the controller HTTP contract.

## Workflow

- Issue-driven: one scoped GitHub issue per task; implement in a branch/worktree off `main`.
- Branch prefix: `<type>/<issue-number>-<short-slug>`, e.g. `feat/3-scan-dry-run`.
- Commits: Conventional Commits.
- Task PRs target `main`; never merge without the operator's explicit go-ahead.
- Do not expand issue scope; hand discovered bugs back to the issue tracker.

## Parallel work (wave 2)

Wave-2 issues run in parallel; land changes only in the area you own.

| Issue | Owns |
| --- | --- |
| #6 worker agent | `src/videocompressor/worker/**`, `src/videocompressor/cli/worker.py`, `tests/worker/**` |
| #8 GUI foundation | `src/videocompressor/gui/**`, `src/videocompressor/cli/gui.py`, `src/videocompressor/core/config.py` additions, `tests/gui/**` |
| #11 pilot dry run | `docs/pilot-notes.md` |

The worker's per-node config file lives under `worker/**` (#6); additions to the per-user
config belong to #8. Additions to `core/**` outside #8's config scope need a docs change first.

Shared append points — single-line additions only:

- `tests/conftest.py` — append your fixture domain to `pytest_plugins`; never grow the file.
- `docs/architecture.md` — add your module to the module map when you add one.
- `tests/test_architecture.py` — size-allowlist entries; numbers may only move down.

Rules:

- Extend by adding a module, not by growing a shared one. Source files stay ≤ 400 lines and
  test files ≤ 450; `tests/test_architecture.py` enforces both.
- Keep append points to single-line additions; never refactor another issue's area in your PR.
- Rebase on `main` before opening a PR.

## Validation

```sh
ruff check .
pytest
```

Integration tests that need ffmpeg/ffprobe are marked `slow`; unit tests must stay fast and
fixture-based.

`tests/test_architecture.py` guards import direction, PySide6 confinement, and file-size budgets
(static and fast); keep it green.
