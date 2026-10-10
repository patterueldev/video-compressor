# Video Compressor

A GUI-driven, resumable GoPro video compressor with optional worker nodes. Drag a mounted
footage folder into the app, review and edit what will be processed, and let one or more
workers replace validated sources — later imports skip clips that are already complete.

[plan.md](plan.md) is the source of truth for product behavior, processing flow, safety rules,
and acceptance criteria; [docs/architecture.md](docs/architecture.md) fixes the module
boundaries, storage/transport seams, and the controller HTTP contract.

## Requirements

- Python 3.12 or newer.
- FFmpeg (`ffmpeg` and `ffprobe`) on `PATH`.
- macOS with the optional `gui` extra (PySide6) for the graphical app. The engine, the CLI,
  and a Linux worker node run without it.

## Install

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'    # engine, CLI, and tests
.venv/bin/pip install -e '.[gui]'    # add the PySide6 GUI
```

The `vc` entry point lives in the venv: `.venv/bin/vc --help`.

## Quick start (GUI)

1. Mount the archive share in macOS, then start the app:

   ```sh
   .venv/bin/vc gui
   ```

   The GUI process is also the controller; a `vc controller` already serving the configured
   address is attached to instead.

2. Drag a footage folder (or use the folder picker), select the processing template, and review
   the archive root. The scan classifies every clip as new, complete, changed, or review, and
   shows capture time, bucket, bitrate, and estimated output size.
3. Edit capture times/bitrates where needed and start a dry run. The job monitor shows workers,
   progress, ETA, and the run log; stopping is safe (the active clip is requeued and restarts).

## CLI

| Command | Purpose |
| --- | --- |
| `vc probe FILE` | Read one clip's capture time, mode, bucket, and review reason. |
| `vc scan --root PATH --folder PATH [--root-id ID] [--report PATH]` | Read-only dry run over a folder; writes only the report. |
| `vc controller [--host H] [--port P] [--print-token]` | Serve the local controller (the GUI can do this instead). |
| `vc worker --config PATH [--once]` | Run one processing node (see the worker runbook). |
| `vc gui [--config PATH] [--manifest PATH]` | Launch the graphical app (needs the `gui` extra). |

## Worker nodes

Workers pull jobs from the controller over the local network and resolve each clip through
their own archive-root mapping, so the same archive-relative path works on the Mac and on
another machine. Each node runs at most one FFmpeg conversion at a time.

- [docs/worker-runbook.md](docs/worker-runbook.md) — native node configuration, start/stop,
  resume and recovery semantics, troubleshooting.
- [deploy/homeserver/README.md](deploy/homeserver/README.md) — the container-based homeserver
  deployment (compose stack, logs, operator checkpoint).

Workers receive the access token out of band (`VC_WORKER_TOKEN`); the controller prints it with
`vc controller --print-token`. Never commit a token or a real archive path.

## Configuration and state

- Per-user config (macOS):
  `~/Library/Application Support/Video Compressor/config.toml` — capture-time mode, named
  encoding templates, registered archive roots, and the controller bind address/token.
- Queue manifest: `manifest.sqlite` in the same folder; the controller owns it while a run is
  active, and dry runs only read it.
- Per-node worker config: separate TOML file owned by each worker (see the runbook).

## Safety notes

- Dry-run first: scans never modify the archive.
- Replacement happens only after the output validates and transfers back; the original is kept
  as a recoverable backup until the replacement is confirmed readable, and a source that changed
  since it was claimed goes to review instead of being overwritten.
- Scratch space must hold the source, the temporary output, pass logs (256 MB allowance), and a
  1 GB margin; one clip per node at a time.
- Reruns skip valid completed clips, including after a template change; changed sources are
  flagged for review (`scripts/rerun_matrix.py` is the evidence check).

## Development

```sh
.venv/bin/ruff check .
.venv/bin/pytest              # fast unit and contract tests, no ffmpeg needed
.venv/bin/pytest -m slow      # integration tests: real ffmpeg/ffprobe media
```

## Docs

- [plan.md](plan.md) — product spec and acceptance criteria.
- [docs/architecture.md](docs/architecture.md) — module boundaries, seams, controller HTTP v1.
- [docs/worker-runbook.md](docs/worker-runbook.md) — run a worker node.
- [docs/pilot-notes.md](docs/pilot-notes.md) — pilot dry run, transport decision, rerun matrix.
- [docs/v1-readiness.md](docs/v1-readiness.md) — the V1 checklist and operator milestones.
