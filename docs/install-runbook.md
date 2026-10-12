# Install runbook

How to install the operator app (macOS) and worker nodes (macOS and Linux): a native install
(virtualenv or pipx), the per-user worker service, and installing a node on another machine over
SSH. For what a node does at runtime — configuration reference, stop/resume/recovery semantics,
troubleshooting — read [worker-runbook.md](worker-runbook.md); the product behavior lives in
[plan.md](../plan.md).

The package is **not published to PyPI**: install it from a checkout or a git ref (commands
below). Worker nodes never need the GUI extra (PySide6); only the operator machine that runs the
app does.

## Prerequisites

Every machine needs:

- **Python 3.12 or newer** with `venv`/`ensurepip`: check `python3 --version` and
  `python3 -m venv --help`. Distributions that still ship an older Python need a newer one
  installed first (a distribution package, `deadsnakes` on Ubuntu, or a python.org/pyenv build).
  `vc node probe` reports the remote Python version and venv availability.
- **FFmpeg** (`ffmpeg` and `ffprobe`) on `PATH`, built with libx265: check `ffmpeg -version` and
  `ffprobe -version`. macOS: `brew install ffmpeg`. Debian/Ubuntu: `sudo apt install ffmpeg`
  (the distribution build includes libx265).
- **Scratch space on a local disk** — never the archive. Per job it holds the source copy, the
  temporary output, the two-pass logs (256 MB allowance), and a 1 GB margin.
- **A route to the archive** the node processes (v1 nodes use a mounted or local path; see
  [pilot-notes.md](pilot-notes.md) for the transport decision) and, for workers, **a route to
  the controller** (see [security-notes.md](security-notes.md) for the bind/firewall guidance).

## Install the package

### Virtualenv (all platforms)

```sh
git clone https://github.com/patterueldev/video-compressor.git
cd video-compressor
python3 -m venv .venv
.venv/bin/pip install -e '.[gui]'    # operator machine: engine, CLI, and GUI
```

On a worker node the GUI extra is not needed:

```sh
.venv/bin/pip install -e .
```

The `vc` entry point lives in the venv (`.venv/bin/vc --help`). Service units run absolute paths,
so keep the venv in a stable location.

### pipx (worker nodes and the CLI)

[pipx](https://pipx.pypa.io/) keeps the CLI available without a project virtualenv:

```sh
pipx install '/absolute/path/to/video-compressor'          # worker node / CLI
pipx install '/absolute/path/to/video-compressor[gui]'     # add the GUI extra (operator machine)
pipx install 'video-compressor @ git+https://github.com/patterueldev/video-compressor@<ref>'
```

The first two forms install from a checkout; the last installs a pinned git ref (replace `<ref>`
with a tag, branch, or commit). Because the package is not on PyPI, no bare
`pipx install video-compressor` exists. Upgrades use `pipx install … --force`.

## Configure a worker node

A node owns one TOML file (`worker.toml`); the controller never reads it. Minimal example —
values are placeholders:

```toml
[worker]
id = "node-a"                          # [A-Za-z0-9_.-]{1,64}; appears in logs and the GUI
name = "Processing node A"             # optional display name
controller_url = "http://controller-host.example.com:8765"   # no path; /api/v1 is appended
token_env = "VC_WORKER_TOKEN"          # or token = "…" (never commit a token)
scratch_dir = "/path/to/scratch"       # local disk, never the archive
thread_cap = 2                         # FFmpeg threads for this machine

[roots]
archive = "/path/to/archive"           # one entry per controller root id
```

Get the token on the controller machine with `vc controller --print-token` and pass it to the
node out of band (environment variable, not the repository). The full key reference — optional
`poll_seconds`, `heartbeat_seconds`, destination root mappings, pilot mode — is in
[worker-runbook.md](worker-runbook.md).

## Run a node

```sh
# Foreground node (Ctrl-C stops it safely; SIGTERM works too)
VC_WORKER_TOKEN=… .venv/bin/vc worker --config /path/to/worker.toml

# Claim and process at most one job, then exit
VC_WORKER_TOKEN=… .venv/bin/vc worker --config /path/to/worker.toml --once
```

Log lines go to stderr; stop semantics, scratch behavior, and recovery are documented in
[worker-runbook.md](worker-runbook.md).

## Service mode (macOS launchd, Linux systemd)

Install the worker as a per-user service that keeps running without a terminal. The service runs
exactly the foreground command (`vc worker --config PATH`); the config file is the service's
identity and must stay in place until the service is uninstalled.

```sh
# Create (or refresh) the unit and start it; idempotent - an unchanged config is a no-op
vc worker install-service --config /path/to/worker.toml

# Report the state; exit code 0 also covers "not_installed"
vc worker service-status --config /path/to/worker.toml
vc worker service-status --config /path/to/worker.toml --json   # schema 1, for automation

# Stop the service and remove the unit; safe to run twice ("absent")
vc worker uninstall-service --config /path/to/worker.toml
```

Add `--dry-run` to `install-service` / `uninstall-service` to print the planned unit and commands
without changing anything (token values are redacted). `service-status` exits `0` when the service
is simply not installed; `1` means the platform command failed (or the platform is unsupported);
`2` means the config or the flags are wrong.

| | macOS (launchd) | Linux (systemd) |
| --- | --- | --- |
| Scope | per-user LaunchAgent | systemd user unit |
| Unit | `~/Library/LaunchAgents/com.videocompressor.worker.<id>.plist` | `~/.config/systemd/user/videocompressor-worker-<id>.service` |
| Logs | `~/Library/Logs/VideoCompressor/<id>.out.log` and `.err.log` | the journal: `journalctl --user -u videocompressor-worker-<id>.service` |
| Restart | on failure, throttled to 30 s | `Restart=on-failure`, `RestartSec=30` |

Notes:

- The service runs as the logged-in user; no sudo is used or required. On a headless Linux host
  run `loginctl enable-linger <user>` once, or the unit stops with the session. macOS
  LaunchAgents need a logged-in user session; headless macOS nodes (system scope) are a follow-up.
- `install-service` validates the config fully (roots, scratch, token) before writing anything;
  `service-status` and `uninstall-service` only read the worker id, so they still work when a
  root is unmounted or the token environment variable is gone.
- Relative paths in the config resolve against the config's own directory in service mode; use
  absolute paths in `worker.toml`.
- Configs that use `token_env`: the resolved value is materialized into the service environment
  at install time (a 0600 `EnvironmentFile` on Linux, `EnvironmentVariables` in the 0600 plist on
  macOS) and is never printed. Uninstall removes the Linux env file even if the config later
  switched to an inline token. **Rotating the token requires re-running `install-service`.**
- Reinstalling an identical unit on a running service performs no command; a changed unit is
  rewritten and the service restarted. The launchd log files grow until macOS log rotation
  handles them; the systemd journal is bounded by journald.

## Remote bootstrap over SSH (`vc node`)

With key-based SSH access, `vc node` installs and manages the worker from another machine without
hand-editing anything on the node:

```sh
vc node probe --host node.example.com --user operator \
  --scratch-dir /path/to/scratch    # platform, Python, ffmpeg, worker/service, free disk
vc node install --host node.example.com --user operator \
  --controller-url http://controller-host.example.com:8765 \
  --token-env VC_WORKER_TOKEN \
  --root archive=/path/to/archive \
  --scratch-dir /path/to/scratch
vc node status --host node.example.com
vc node uninstall --host node.example.com                       # add --purge to remove the install dir
```

The remote host needs Python 3.12+ with venv/ensurepip and ffmpeg on `PATH`; `probe` checks all
three (and free disk space for `--scratch-dir`) before anything is installed. Connection options
(`--user`, `--port`, `--identity`, `--connect-timeout`) are shared by all four actions, and each
accepts `--install-dir` (default `~/.videocompressor`) and `--json`.

- Everything managed lives under `~/.videocompressor/` on the node (venv, staging, `worker.toml`
  at mode 0600). The install ships a locally prepared wheelhouse, so the node needs no network
  and no build toolchain; the token comes from `--token-env` or `--token-file` on the machine
  running the command and never appears in output. No sudo is used or required (the service
  itself is the per-user `vc worker install-service` above).
- Host keys: `accept-new` by default (first contact is pinned, later changes fail); pass
  `--strict-host-keys` to require a pre-seeded `known_hosts`. Auth is key-based only; see
  [security-notes.md](security-notes.md).
- `install` accepts `--worker-id` (default: the remote hostname), `--name`, `--thread-cap`
  (default 2), `--python`, and `--wheelhouse`; repeat `--root ID=PATH` for every controller
  root the node resolves.
- Re-running `install` on an existing node force-reinstalls the staged wheel even when its
  version is unchanged, so the remote code always matches the wheelhouse; the `install` step
  reports `unchanged` only when pip installed nothing. A `--wheelhouse` used for an update must
  also contain the project's dependency wheels (the default, building one, does).
- Uninstall stops the service and removes the worker config; `--purge` also removes
  `~/.videocompressor/`. Root mappings, scratch content, and the archive are never deleted.

The app's Nodes tab wraps the same backend: add a node by host/IP with its SSH user and
identity, map each registered root to that host's path, set scratch and thread cap, then probe,
install or update it; removing a node can uninstall the worker service on the host first. The
entries persist in the per-user config (`[[nodes]]`), and the CLI above remains the scriptable
fallback.

## Container alternative (Linux)

The Linux homeserver can also run as a Docker container instead of a native service; the compose
stack, logs, and two-node check live in
[deploy/homeserver/README.md](../deploy/homeserver/README.md). Use the native service when the
machine should keep the node running like any other per-user service; use the container when
Docker already manages the host's services.

## Verify the install

- `vc node probe --host …` (remote) or `vc worker service-status --config …` (local) reports the
  platform, Python, ffmpeg, worker, and service state.
- In the app, the Monitor shows the node as online with its roots; a job sent from the Convert
  tab is claimed by it.
- If the node does not register, see the troubleshooting table in
  [worker-runbook.md](worker-runbook.md) and the controller bind guidance in
  [security-notes.md](security-notes.md).
