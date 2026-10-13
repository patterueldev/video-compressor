# Worker runbook

How to run a processing node: configuration, start/stop, and what happens to an active clip when
a worker stops, crashes, or loses the controller. Read [plan.md](../plan.md) for the product
behavior and [docs/architecture.md](architecture.md) for the controller contract first.

The controller (the GUI process or a `vc controller` run) owns the durable queue. Workers never
talk to each other: they register with the controller, claim one queued clip at a time, and
report the terminal state. **Each node runs at most one FFmpeg conversion at a time**, and the
controller guarantees that no clip is claimed twice.

For the container-based homeserver node, see
[deploy/homeserver/README.md](../deploy/homeserver/README.md); the semantics below apply to it
too.

## Configuration (`worker.toml`)

The node owns this file; the controller never reads it. Values below are placeholders.

```toml
[worker]
id = "node-a"                          # [A-Za-z0-9_.-]{1,64}; appears in logs and the GUI
name = "Processing node A"             # optional display name
controller_url = "http://controller-host:8765"   # no path; the client appends /api/v1
token_env = "VC_WORKER_TOKEN"          # or token = "…" (never commit a token)
scratch_dir = "/path/to/scratch"       # local disk, never the archive
thread_cap = 2                         # FFmpeg threads for this machine
poll_seconds = 5                       # idle claim interval

[roots]
archive = "/path/to/archive"           # one entry per controller root id

# [pilot]                              # optional: pilot-output mode
# root = "/path/to/pilot-output"       #   delivers validated output here; never replaces
```

Notes:

- `[roots]` keys must match the manifest's registered root ids (for example `archive` or
  `camera`). A claimed job whose root is not mapped fails with a mapping error. Destination
  output roots must be mapped here too: a job writes into its destination only when this node
  can resolve that root.
- `token_env` is preferred; export `VC_WORKER_TOKEN` before starting the worker. Get the token
  from the controller machine with `vc controller --print-token` and pass it out of band.
- `heartbeat_seconds` is optional (default 15 s, adopted from the controller). Lower values make
  the job stage visible faster in the GUI; the controller's orphan grace
  (`[controller] orphan_grace_seconds`, default 300 s) does not change.
- Pilot mode (`[pilot] root`) delivers validated output into a separate tree and parks the clip
  in review with "originals untouched"; it never writes to the archive.

## Scratch space

Scratch must sit on a local disk and hold, per job: the source copy, the encoded output, the
two-pass logs (256 MB allowance), and a 1 GB margin. The worker checks free space before
encoding and fails the job with `scratch space: …` when short. The whole `jobs/` tree is swept at
worker startup, so an interrupted attempt always restarts from the beginning.

## Start, stop, logs

```sh
# Foreground node (Ctrl-C stops it safely; SIGTERM works too)
VC_WORKER_TOKEN=… .venv/bin/vc worker --config /path/to/worker.toml

# Claim and process at most one job, then exit
VC_WORKER_TOKEN=… .venv/bin/vc worker --config /path/to/worker.toml --once
```

Log lines go to stderr (`INFO` level): `registered worker … with controller …`, per-job
`settled: <disposition>` lines, progress warnings, and controller-unavailable retries. The
container stack keeps bounded logs (`docker compose logs -f --tail=100 worker`).

Stop semantics (see "Safely stopping and picking up a node" for the operator procedure):

- **Ctrl-C / SIGTERM / `docker compose stop`**: stop now — the active FFmpeg process is
  terminated, the clip is requeued, and the worker exits; successful clips stay complete.
- **SIGUSR1** (macOS/Linux): stop after the current clip — the active job settles normally and
  the worker exits without claiming another.
- **GUI/monitor stop**: the same two modes for the selected node, "Stop now" and "Stop after
  current"; either exits the worker process (with `restart: unless-stopped` the container comes
  back, so remove/stop the stack to keep it off).
- **`--once`** is for watched/scripted runs: it exits immediately when nothing is claimable.

## Service mode (macOS launchd, Linux systemd)

Install the worker as a per-user service that keeps running without a terminal:

```sh
vc worker install-service --config /path/to/worker.toml    # create (or refresh) and start
vc worker service-status --config /path/to/worker.toml     # report; --json for automation
vc worker uninstall-service --config /path/to/worker.toml  # stop and remove
```

Unit locations, logs, exit codes, `--dry-run`, the headless-host `loginctl enable-linger` note,
and token materialization/rotation are in [install-runbook.md](install-runbook.md). The config
file is the service's identity and must stay in place until the service is uninstalled.

## Safely stopping and picking up a node

To take a node away (for example a laptop that has to leave) without losing work, stop it with a
mode and wait for it to exit:

- **Stop after current (drain)** — the node finishes the clip it is processing, reports it
  normally, and exits without claiming another. Nothing is requeued; use this for a safe pickup.
- **Stop now (cancel)** — the node cancels the active clip (it is requeued and restarts from the
  beginning later) and exits. Use this when the machine must go immediately.

From the Monitor tab, select the node and press the matching button; the status column shows
`draining…` or `stopping…` until the node exits. Both modes also stop an idle node. The request is
persisted on the controller and delivered on the node's next heartbeat (within ~15 s); it survives
a controller restart and clears when the node re-registers or when the operator presses **Resume**.

Local control without the Monitor:

```sh
kill -USR1 <worker-pid>                                                  # drain: finish, then exit
kill -TERM <worker-pid>                                                  # stop now: cancel, then exit
systemctl --user kill -s SIGUSR1 videocompressor-worker-<id>.service    # drain a service node
```

A stop exits with code 0, and the installed service does not restart it (`Restart=on-failure`;
launchd `SuccessfulExit: false`), so the node stays stopped until it is started again.

Starting the node again afterwards:

- App-managed local worker: press **Start** in the Monitor's local-worker strip (or **Save & apply**).
- Installed service: re-run `vc worker install-service --config /path/to/worker.toml` (idempotent,
  starts it), or `systemctl --user start videocompressor-worker-<id>.service` on Linux.
- Foreground node: `VC_WORKER_TOKEN=… .venv/bin/vc worker --config /path/to/worker.toml` again.

A pending stop clears when the node registers again, so a restarted node claims work normally. If
a stop was requested but the node should keep working, press **Resume** in the Monitor before the
node acts on it.

Fleet rollout order for the stop modes: update the controller first (restart the app or
`vc controller`), then the agents (`vc node install …` or `vc worker install-service …`). An agent
that predates the mode contract keeps the old behavior: a stop cancels its active clip and the
request clears when that clip settles; it cannot be stopped while idle.

## Remote bootstrap (`vc node`)

With key-based SSH access, `vc node probe|install|status|uninstall` installs and manages this
worker on another machine from the CLI; the app's Nodes tab drives the same backend. The
command set, host requirements, host-key policy (`accept-new` default, `--strict-host-keys`
opt-in), and uninstall semantics are in [install-runbook.md](install-runbook.md); the security
stance is in [security-notes.md](security-notes.md). Everything managed lives under
`~/.videocompressor/` on the node; no sudo is used or required.

## Stop, resume, and recovery

- The controller gives every claim a job lease of `orphan_grace_seconds` (`[controller]` setting
  on the controller machine, default 300 s = 5 minutes); workers renew it by heartbeat (15 s).
  A node that sleeps inside the window wakes and resumes its job seamlessly; after the window the
  clip is requeued and the woken node's local work is discarded (its next heartbeat gets `409`).
- The Monitor still flips a silent node offline after `min(grace, 90 s)`, so a long grace never
  delays the display even though the job lease is still held.
- Tune the grace by setting `[controller] orphan_grace_seconds` (a positive integer, seconds) in
  the controller machine's per-user config and restarting the app or `vc controller`; a GUI
  attached to an already-running controller uses that controller's value.
- Expired leases in the copy/encode stages are safe to retry: the source was never touched, so
  the controller requeues the clip and a later claim restarts it from scratch.
- Expired leases in the replacement stages (`validated`, `transferring`, `replacing`) park the
  clip in **review**. The next claim runs `core/recover.reconcile_source` first: staged temps are
  removed, a missing source is restored from the backup, a confirmed replacement is kept, and
  ambiguous states (several backups, unreadable replacement) stay in review for the operator.
  Nothing can be claimed twice while a live lease or unresolved recovery exists.
- Replacement artifacts (`.vc-tmp.<job>`, `.vc-backup.<job>`) live beside the source. Do not
  delete them by hand: recovery needs them to prove the source is intact.
- A clip parked in review stays there (and scans keep showing it) until the queue record is
  reset for another attempt — currently with the pilot tooling
  (`scripts/pilot_run.py enqueue --requeue`); the GUI retry action is a tracked follow-up.

## Biasing the worker off under load

- Full off, safe pickup: stop after the current clip (`SIGUSR1` or Monitor "Stop after current") —
  the clip finishes, then the node exits.
- Full off, immediately: stop now (`docker compose stop`, Ctrl-C, or Monitor "Stop now"). The
  active job is requeued.
- Live CPU trim (container): `docker update --cpus=1 <container>`, restore with `--cpus=2`.
- Lighter config: set `thread_cap = 1` and recreate the worker; it cancels and requeues the
  active job.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `ControllerUnavailable` / claim retries | Controller is running and the URL/bind address are reachable (`0.0.0.0` when workers are on other machines); firewall rules. |
| `controller rejected the token` (exit 2) | `VC_WORKER_TOKEN` matches `vc controller --print-token`. |
| `root_id … is not mapped` | `[roots]` key matches the manifest root id; path exists on this node. |
| `scratch space: …` | Move scratch to a volume with source + output + 256 MB + 1 GB free. |
| Clip stuck in review | Inspect the error text and the folder marker/manifest: a changed source or crashed replacement needs `core/recover`/retry; never delete `.vc-backup.*` until recovery reports a confirmed state. |
| Scan shows a clip as new | No marker or manifest record for it (first run, or the files were added later). |
| Worker exits at startup | Run `--once` to see the first error; check the config errors above. |

## Evidence runs

- `python scripts/two_node_check.py` — local native + container concurrency check (issue #10).
- `python scripts/rerun_matrix.py run …` — the four-scenario rerun matrix (issue #13):
  completed-clip skip, changed-source review, interrupted-run resume, failure recovery. With
  `--archive-root` and `--clip` it stages real clips out of the mounted archive (read-only) under
  `--matrix-root`; with `--synthetic` it generates clips. See
  [docs/v1-readiness.md](v1-readiness.md) for the operator checkpoints.
