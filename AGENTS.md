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

## Validation

```sh
ruff check .
pytest
```

Integration tests that need ffmpeg/ffprobe are marked `slow`; unit tests must stay fast and
fixture-based.
