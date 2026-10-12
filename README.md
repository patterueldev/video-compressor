# Video Compressor

A GUI-driven, resumable GoPro video compressor with optional worker nodes. Drag files or folders
into the app (or pick them with the dialogs), choose where the compressed copies go, optionally
delete the verified originals, and let one or more workers share the conversion — later runs skip
clips that are already complete.

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

The `vc` entry point lives in the venv: `.venv/bin/vc --help`. Worker-node installs (macOS and
Linux), per-user services, and remote bootstrap live in [docs/install-runbook.md](docs/install-runbook.md).

## Quick start (convert flow)

1. Mount the archive share in macOS, then start the app:

   ```sh
   .venv/bin/vc gui
   ```

   The GUI process is also the controller; a `vc controller` already serving the configured
   address is attached to instead. Register the source root (and any output root), and review the
   templates, in the Library tab.

2. In the Convert tab, select the videos: drop files and/or folders onto the drop area, or use
   the "Open files…" / "Open folder…" dialogs. A dropped folder recurses and picks up video
   files only; the pending list shows the clip count and total size.

3. Choose the destination — a registered output root (optionally with a subfolder), a custom
   folder inside a registered root, or "same folder as source" (the verified swap, which
   consumes the source) — and decide whether originals are deleted after conversion. The default
   keeps originals; a source is deleted only after its output exists at the destination and
   validates.

4. Press Start. The Monitor tab shows workers, per-node progress, ETA, and the run log; stopping
   is safe (the active clip is requeued and restarts later).

With the default settings the app also runs a local worker on this machine, so a fresh install
converts without starting a node by hand; the local worker appears in the Monitor like any other
node and is controlled from there.

## CLI

| Command | Purpose |
| --- | --- |
| `vc probe FILE` | Read one clip's capture time, mode, bucket, and review reason (optional tooling). |
| `vc scan --root PATH --folder PATH [--root-id ID] [--report PATH]` | Read-only dry run over a folder; writes only the report (optional tooling). |
| `vc controller [--host H] [--port P] [--print-token]` | Serve the local controller (the GUI can do this instead). |
| `vc worker --config PATH [--once]` | Run one processing node (see the worker runbook). |
| `vc node probe/install/status/uninstall --host HOST …` | Install and manage a worker on a remote host over SSH (key-based; see the install runbook). |
| `vc gui [--config PATH] [--manifest PATH]` | Launch the graphical app (needs the `gui` extra). |

Direct conversions are enqueued from the GUI through the controller's `POST /api/v1/jobs` API;
there is no separate CLI enqueue command. `vc scan`/`vc probe` never modify the archive.

## Worker nodes

Workers pull jobs from the controller over the local network and resolve each clip — and any
destination output root — through their own root mapping, so the same archive-relative path works
on the Mac and on another machine. Each node runs at most one FFmpeg conversion at a time.

The app runs a local worker by default (setting: `[local_worker]` in the per-user config), so a
fresh install converts without starting a worker by hand; its node appears in the Monitor, where
it can be stopped or switched to a keep-running service.

- [docs/install-runbook.md](docs/install-runbook.md) — install and manage a node on macOS and
  Linux (virtualenv or pipx, per-user service, remote bootstrap over SSH).
- [docs/worker-runbook.md](docs/worker-runbook.md) — native node configuration, start/stop,
  resume and recovery semantics, troubleshooting.
- [docs/security-notes.md](docs/security-notes.md) — SSH keys and host keys, token handling,
  and controller bind/firewall guidance.
- [deploy/homeserver/README.md](deploy/homeserver/README.md) — the container-based homeserver
  deployment (compose stack, logs, operator checkpoint).

Workers receive the access token out of band (`VC_WORKER_TOKEN`); the controller prints it with
`vc controller --print-token`. Never commit a token or a real archive path; see the
[security notes](docs/security-notes.md).

## Configuration and state

- Per-user config (macOS):
  `~/Library/Application Support/Video Compressor/config.toml` — capture-time mode, named
  encoding templates, registered roots (sources and destinations), and the controller bind
  address/token.
- Queue manifest: `manifest.sqlite` in the same folder; the controller owns it while a run is
  active, and dry runs only read it.
- Per-node worker config: separate TOML file owned by each worker (see the runbook); its
  `[roots]` mapping must cover every root the node needs — source roots and destination roots.

## Safety notes

- Deleting originals is off by default. Nothing is replaced or deleted before its output exists
  at the destination and passes validation.
- Replacement/placement happens only after the output validates; a source that changed since it
  was claimed goes to review instead of being overwritten or deleted, and a same-folder swap
  keeps a recoverable backup until the replacement is confirmed readable.
- Scratch space must hold the source, the temporary output, pass logs (256 MB allowance), and a
  1 GB margin; one clip per node at a time.
- Reruns skip valid completed clips, including after a template change; changed sources are
  flagged for review (`scripts/rerun_matrix.py` is the evidence check).
- Dry runs (`vc scan`) never modify the archive.

## Development

```sh
.venv/bin/ruff check .
.venv/bin/pytest              # fast unit and contract tests, no ffmpeg needed
.venv/bin/pytest -m slow      # integration tests: real ffmpeg/ffprobe media
```

## Docs

- [plan.md](plan.md) — product spec and acceptance criteria.
- [docs/architecture.md](docs/architecture.md) — module boundaries, seams, controller HTTP v1.
- [docs/install-runbook.md](docs/install-runbook.md) — install the app (macOS) and worker nodes
  (macOS and Linux), service management, remote bootstrap.
- [docs/worker-runbook.md](docs/worker-runbook.md) — run a worker node.
- [docs/security-notes.md](docs/security-notes.md) — SSH, token, and controller-bind guidance.
- [docs/pilot-notes.md](docs/pilot-notes.md) — optional dry runs, transport decision, rerun matrix.
- [docs/v1-readiness.md](docs/v1-readiness.md) — the V1 checklist and operator milestones.
