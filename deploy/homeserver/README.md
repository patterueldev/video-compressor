# Homeserver worker — container deployment runbook (issue #10)

Run the processing node as a small Docker container on the homeserver: Python 3.12, Debian
ffmpeg (with libx265), and the `video-compressor` package (no GUI extra). The worker registers
with the controller running on the operator machine, claims one clip at a time, copies it to
local scratch, encodes, validates, and replaces the source through the verified protocol.

> Once the worker-fleet work lands (epic #50; end-to-end acceptance in #56), the app's Nodes
> panel installs and manages a worker on this host over SSH, superseding the manual first-time
> steps below. This document remains the container-based Linux alternative and reference; the
> native path is in [docs/install-runbook.md](../../docs/install-runbook.md).

Container paths used by the stack:

| Purpose | Host path (from `.env`) | Container path | Mode |
| --- | --- | --- | --- |
| Worker config | `./worker.toml` | `/config/worker.toml` | ro |
| Archive root | `VC_ARCHIVE_ROOT` | `/srv/archive` | rw |
| Scratch (internal disk) | `VC_SCRATCH_ROOT` | `/var/lib/videocompressor/scratch` | rw |

The archive mount is **rw** because the worker stages a temporary output and a recoverable
backup beside the source. Scratch must be on the **internal disk** (not the archive drives):
it needs room for one source copy, the temporary output, and the pass logs, and it is wiped
after each job.

## First-time setup

1. Get the controller token on the machine running the GUI/controller:

   ```sh
   vc controller --print-token
   ```

   The token stays out of band: it goes into the untracked `.env` file only, never into the
   repository or `worker.toml`.

2. Copy and edit the local files in this directory:

   ```sh
   cp .env.example .env
   cp worker.example.toml worker.toml
   ```

   - `.env`: paste `VC_WORKER_TOKEN`; set `VC_ARCHIVE_ROOT` and `VC_SCRATCH_ROOT` to real host
     paths; set `VC_UID` / `VC_GID` to the uid/gid that owns the archive files (`id -u`,
     `id -g`). Empty required values make compose fail fast with a clear message.
   - `worker.toml`: point `controller_url` at the controller machine
     (`http://<controller-host>:8765`), keep `token_env = "VC_WORKER_TOKEN"`, and keep
     `thread_cap = 2` on the 2-core homeserver (the issue allows 2–3). The `[roots]` mapping
     uses the container path `/srv/archive`.

3. Build and start (build on the target machine, not on a different architecture):

   ```sh
   docker compose build
   docker compose up -d
   docker compose ps
   ```

   The controller should be running first. If it is not reachable yet, the worker exits and
   `restart: unless-stopped` retries until registration succeeds; check the logs if it keeps
   flapping.

## Logs

```sh
docker compose logs -f --tail=100 worker
```

Worker output goes to the container's stderr and is kept in bounded json-file logs (10 MB × 3).
Look for `registered worker ... with controller ...`, per-job `settled:` lines, and warnings
about an unreachable controller.

## Start, stop, restart

| Action | Command | Effect |
| --- | --- | --- |
| Start | `docker compose start` | Starts the stopped container. |
| Stop | `docker compose stop` | SIGTERM: the active job is cancelled, requeued, and restarts from scratch later; the container exits 0. |
| Restart | `docker compose restart` | Stop + start. |
| Remove | `docker compose down` | Stops and removes the container (the image and local files stay). |

Successful clips stay complete across restarts; an interrupted clip is requeued and reprocessed
from the beginning by either node.

## Biasing the worker off under load

- **Full off (recommended):** `docker compose stop` — no new claims and the active job is
  requeued; `docker compose start` resumes later.
- **Live CPU trim:** `docker update --cpus=1 <container>` (no restart); restore with
  `docker update --cpus=2 <container>`.
- **Lighter config:** set `thread_cap = 1` in `worker.toml` and `docker compose up -d` to
  recreate the container; it also cancels and requeues the active job.
- Note: the GUI/controller stop endpoint cancels the active job and the worker process exits;
  with `restart: unless-stopped` the container comes back, so use `docker compose stop` when
  the node must stay off.

## Troubleshooting

- **Container restart loop with `ControllerUnavailable`**: the controller is down or not
  reachable at `controller_url`; the loop converges once it is up.
- **Exit 2 with "controller rejected the token"**: the token in `.env` does not match
  `vc controller --print-token`; update `.env` and `docker compose up -d`.
- **Compose fails with "required variable ... is missing a value"**: a required value in `.env`
  is empty; fill it in.
- **Archive writes fail with permission errors**: set `VC_UID` / `VC_GID` to the archive owner
  and recreate the container.

## Two-node concurrency check

The repository ships a local check that runs one native node and one container node against a
single controller on two generated clips and verifies: both nodes encode simultaneously on
different clips, each clip is claimed exactly once by a distinct worker, both complete with
HEVC 720p output and recorded fingerprints, and no swap artifacts are left. Run it from the
project virtualenv on a machine with Docker and ffmpeg:

```sh
python scripts/two_node_check.py
```

Use `--skip-container` to run only the native leg, `--keep` to keep the workdir and node logs,
and `--target <dir>` to choose where the workdir is created (must be a Docker-bindable path,
for example under the home directory on macOS).

For the real deployment, the same properties are checked on real clips: run the container node
against the real controller and watch the GUI; record log excerpts and the homeserver's
memory/swap behavior (see below).

## Operator checkpoint (real homeserver run)

These steps need the real homeserver and are recorded on issue #10:

1. Reconcile this compose file with the homeserver repository conventions (project name, volume
   layout, uid/gid, restart policy).
2. Clone the merged commit on the homeserver, create `.env` and `worker.toml` from the
   templates, and `docker compose up -d`.
3. Run the two-node check with real clips (one node on the Mac, the homeserver container on a
   different clip): one FFmpeg per node, no duplicate claims, both complete and verified; keep
   worker and GUI log excerpts.
4. Record responsiveness during the run: `free -m` and swap in/out, `uptime` load, and the
   kernel log for OOM events.
