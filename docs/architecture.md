# Architecture

Read [plan.md](../plan.md) first; it is the source of truth for behavior. This document fixes the
module boundaries, the storage/transport seams, and the controller HTTP contract v1 that later
issues implement against.

## Modules and import rules

| Package | Responsibility | May import |
| --- | --- | --- |
| `videocompressor.core` | Engine: probing, capture time, fingerprints, scan/classification, manifest, encoding, validation, replacement. | stdlib and third-party libs (never PySide6) |
| `videocompressor.controller` | Local HTTP controller: durable queue, worker registry, leases, dispatch, run log. Hosted inside the GUI process. | `core` |
| `videocompressor.worker` | Worker agent: register/heartbeat/claim loop and the per-job pipeline; at most one FFmpeg conversion per node. | `core` |
| `videocompressor.node` | Remote node bootstrap over SSH: probe, install, configure, start, and uninstall a worker on another machine (issue #53). | `worker` |
| `videocompressor.gui` | PySide6 app; a thin client that also hosts the controller in-process. | `core`, `controller`, `node` |
| `videocompressor.cli` | `vc` entry points wiring the packages together. | all of the above (PySide6 import stays lazy) |

Rules:

- `core` must never import `controller`, `worker`, or `gui` — the engine must outlive the GUI
  (plan.md hard requirement).
- Only `gui` imports PySide6, and PySide6 is installed only through the `gui` extra; CI and the
  worker never need it.
- CLI subcommands are stubs until their owning issue lands; each exits with code 2 and a clear
  "not implemented" message. `vc probe` (issue #2) and `vc scan` (issue #3) are implemented and
  read-only: `probe` reports one clip's capture time, mode, bucket, and review reason; `scan`
  classifies a footage folder and writes only its dry-run report. `vc controller` (issue #7)
  serves the controller contract below; `vc gui` (issue #8) launches the GUI; `worker` (issue #6)
  runs the node agent.
- These rules are enforced statically by `tests/test_architecture.py`: cross-package import
  direction (the table above), PySide6 only under `gui/`, and the file-size budgets with their
  shrink-only allowlist.

### Module map

Post-wave-1 layout (issues #20–#22 split the CLI, the controller server, and the test fixtures):

- `core`: `models` (dataclasses/enums), `probe`, `capture_time`, `fingerprint`, `scan`,
  `manifest` (SQLite queue), `manifest_schema` (queue DDL, migration, row codecs), `markers`,
  `ffmpeg` (two-pass x265 encode), `validate`, `replace`, `place` (destination placement),
  `records` (destination completion records), `recover`, `config` (per-user TOML),
  `nodes` (per-node entries: SSH target, root mappings; issue #54).
- `controller`: `server` (facade), `http` (HTTP plumbing),
  `registry`/`jobs`/`enqueue`/`sweep` (mixins), `store` (SQLite bookkeeping),
  `memory_manifest` (in-memory double/fallback), `protocol` (payload rules), `constants`.
- `gui`: `app`, `main_window`, `drop_zone`, `roots_panel`, `templates_panel`, `clip_tables`, and
  `scan_runner` widgets plus the pure Python `state` and `scan_service` (issue #8) and the job
  monitor (`monitor_app`, `monitor_window`, `monitor_panel`, `monitor_tables`, `monitor_runner`,
  `monitor_service`, `monitor_client`, `controller_session`; issue #9) and the convert flow
  (`convert_panel`, `convert_destination`, `convert_drop` widgets plus the pure Python
  `convert_service`; issue #41), the Nodes panel (`nodes_panel`, `nodes_dialog`,
  `nodes_tables`, `nodes_runner`, `nodes_service`; issue #54), and the app-managed local
  worker (`local_worker` supervisor, `local_worker_config` generation,
  `local_worker_service` subprocess seams, plus the `local_worker_panel` strip; issue #52).
- `worker`: `agent` (register/heartbeat/claim loop and stop), `client` (controller HTTP v1),
  `outcome` (per-job result type), `pipeline` (per-job copy → encode → validate → replace),
  `destination` (destination decode/resolve/place/delete), `transfer` (pilot delivery and
  replacement confirm), `config` (per-node TOML), `scratch` (local job dirs), and
  `units`/`service`/`service_platform` (launchd/systemd unit rendering and service
  install/status/uninstall; issue #51).
- `node`: `models` (host/probe/spec/report types), `ssh` (OpenSSH runner seam), `probe`
  (read-only host report), `config` (worker TOML render/push), `service` (frozen #51 CLI
  adapter), `distribution` (wheelhouse build/validate), `bootstrap` (`vc node` orchestrator;
  the #54 backend).
- `cli`: `app` (parser + command registry), one module per subcommand (`probe`, `scan`,
  `worker`, `node`, `controller`, `gui`), `constants` (exit codes).

### Remote node bootstrap (issue #53)

`videocompressor.node` drives a worker on another machine over SSH through the `SshRunner` seam
(system OpenSSH binaries; faked in every test). The distribution artifact is a wheelhouse shipped
over SSH (the node needs no network or build toolchain), and host keys default to `accept-new`
with `--strict-host-keys` as the opt-in. The remote service operations call the
`vc worker install-service / service-status / uninstall-service --config PATH` surface frozen by
issue #51, isolated in `node/service.py`. The #54 Nodes panel uses this package as its backend;
no SSH logic lives under `gui/`. The Nodes tab (`gui/nodes_panel.py`, issue #54) wires
`NodeBootstrap` behind a fakeable factory and stores one `[[nodes]]` entry per host in the
per-user config.

### App-managed local worker (issue #52)

The GUI runs a worker on its own machine by default (`[local_worker]` in the per-user config,
`enabled = true`). The supervisor (`gui/local_worker.py`) generates
`<config dir>/local-worker.toml` from the registered roots — each root mapped to this machine's
path — and starts it as a child process managed by the app (the same `vc worker --config PATH`
argv as the service); "keep running when the app closes" switches to the #51 service through the
frozen `vc worker install-service / service-status / uninstall-service --config PATH --json`
commands. The token travels in `VC_WORKER_TOKEN` only and is never written into the generated
file. Startup is idempotent: an online worker with the same id (or the same root mapping) is
adopted, and a registration row whose last heartbeat predates a confirmed app stop is treated as
dead instead of blocking the default flow. Closing the app terminates the child (the active clip
requeues) unless keep-running leaves the service up; the local node appears in the Monitor like
any other node.

### Test layout

`tests/conftest.py` is a registrar only: fixture groups live in `tests/fixtures/<domain>.py` and
are registered through `pytest_plugins`. Suites are the top-level `tests/test_*.py` files and
`tests/cli/**`; `tests/worker/**` (issue #6) and `tests/gui/**` (issues #8, #9, and #54) hold the per-area
suites.
`tests/test_imports.py` checks that every module imports without the `gui` extra;
`tests/test_architecture.py` checks cross-package import direction and file-size budgets
(`src/**` ≤ 400 lines, `tests/**` ≤ 450). The audited over-budget files may only shrink:
`core/manifest.py` (476), `core/scan.py` (464), `core/replace.py` (471), `core/ffmpeg.py` (470)
at #39; the recommended first seam for `manifest.py` was the DDL/schema plus row codec, now
extracted into `core/manifest_schema.py`.

## Data flow

The convert flow is the product's primary flow (epic #38): selected videos (drag & drop or open
dialogs) → explicit files → `POST /api/v1/jobs` (issue #39) → queue (manifest) → worker claims a
job (#7) → copy to local scratch → two-pass x265 encode (#4) → validate (#4) → destination
placement (#40) or the same-folder verified swap (#5) → optional verified source delete (#40) →
complete/skip in the manifest → run summary and monitor (#9).

Convert jobs (#40) carry a destination — a registered output root, a custom folder, or the
source's own folder — and delete the original only after the destination file exists and
validates (see "Destination placement, skip, and verified delete"). A job's identity stays the
`root_id` + `relative_path` pair; enqueue is idempotent, so re-selecting a clip never rewrites an
existing record.

Optional tooling, no longer the main path: a dropped folder → scan + classify (#2, #3) → dry-run
report. Legacy or script-seeded clips without a destination keep the replace-at-source flow.

The GUI process is the controller and owns the durable queue and manifest while a run is active.
Workers pull jobs and resolve each job's archive-relative path through their own configured
archive-root mapping, so the same relative path works on the Mac, another computer, or the Linux
homeserver.

## Archive scan artifacts (issue #3, optional tooling)

Scanning is no longer the product's primary flow (see "Data flow"); this section documents the
optional dry-run and reporting tooling.

- The manifest is keyed by `root_id` + archive-root-relative POSIX path. Until roots are
  registered (issue #8), the CLI derives a stable `root_id` from the resolved root path and can
  pin one with `vc scan --root-id`; scans only ever read inside the root (symlinked files and
  directories are skipped).
- `core/scan.py` classifies each clip as new, complete, changed, or review. A dry run writes
  only its report: the manifest is opened read-only and folder markers are never written by a
  scan.
- Completion is confirmed by size + sampled digest rather than mtime, because modification-time
  precision differs between SMB mounts and local filesystems. Either the central manifest or the
  per-folder `manifest.json` marker (written by the replacement pipeline in #5/#6) is enough to
  skip a valid completed clip: losing one source of truth never forces a re-encode, and a later
  template change never invalidates a completed clip. A recorded completion that no longer
  matches is `changed` and goes to review, never a silent replace.
- Capture times come from issue #2's resolver (`core/capture_time.py`) through the `core/scan.py`
  inspector; missing or conflicting timestamps route clips to review. Tests inject fake inspectors
  or fake probe results, so the scan suite stays ffprobe-free.
- The manifest `events` table (seq/at/level/message) records engine-level events; the controller
  keeps its per-run log (worker and run tagging) in its own database, served by
  `GET /api/v1/events`. `LEASE_TTL_SECONDS` (90) and `HEARTBEAT_INTERVAL_SECONDS` (15) live in
  `core/manifest.py` and are re-exported by `controller/constants.py`.

## Access-transport seam

Everything above the seam works with local file paths only. The worker pipeline reaches the
archive through three primitive operations, implemented against the worker's resolved local path:

| Primitive | Contract |
| --- | --- |
| `fetch_source(relative_path, scratch_dir)` | Copy the source clip into local scratch and verify the copy. |
| `put_back(local_output, relative_path)` | Copy the encoded output to a unique temp file beside the source on the archive filesystem; verify the transfer by read-back. |
| `replace(temp_path, relative_path)` | Fingerprint pre-check → rename source to a recoverable backup → atomic rename temp into place → confirm readable → delete backup. |

v1 implements these directly on the worker-local filesystem: the SMB mount on macOS, the local
ext4 path on the Linux homeserver. The access-transport decision (issue #13) kept mounted/local
paths for every v1 node; the three primitives stay the seam for a possible SSH/rsync transport,
without touching scan, encode, or replacement logic (rationale in `docs/pilot-notes.md`).
`replace` is implemented in `core/replace.py` (issue #5); the pipeline wiring in issue #6, with
`core/recover.py` handling crash reconciliation.

### Replacement artifacts and recovery

Both the staged temp and the backup live in the source's directory, so every rename stays on one
filesystem and is atomic. Names embed a per-attempt job id (`[A-Za-z0-9._-]{1,64}`) so concurrent
jobs cannot collide:

| Artifact | Name |
| --- | --- |
| Staged output | `<clip name>.vc-tmp.<job_id>` |
| Source backup | `<clip name>.vc-backup.<job_id>` |

No attempt (including a retry after lease recovery) may start while artifacts for the clip exist:
`core/replace.py` refuses with `ArtifactsPresentError`, and `core/recover.reconcile_source` runs
first. Recovery is idempotent and never leaves the source path without the original or a
confirmed replacement:

| On-disk state | Reconciliation |
| --- | --- |
| source + temp(s) | remove the temps; the original was untouched |
| source missing + backup | rename the backup back to the source path |
| source missing + backup + temp(s) | rename the backup back; remove the temps |
| source + backup, replacement readable | delete the backup (replacement confirmed) |
| source + backup, replacement unreadable | rename the backup back over the replacement |
| source missing, no artifacts | report `source_missing` for review; nothing can be restored |
| several backups | restore the newest if needed, park the rest as `.vc-review.*`, report review |

The readability confirmation defaults to a minimal `ffprobe` check; issue #4's full validator can
be passed instead through the same seam.

### Destination placement, skip, and verified delete (issue #40)

Convert jobs may carry a destination — a registered output root plus relative path, a custom
folder on the node, or the source's own folder (which is the verified swap above) — and a
`delete_original` flag. The worker decodes and resolves the claim spec per node
(`worker/destination.py`); a custom destination is placed by `core/place.py`:

1. stage `<clip name>.vc-tmp.<job_id>` beside the destination file, verify it by full read-back
   SHA-256, and validate it at the destination with the full validator;
2. write `<clip name>.vc-meta.json` (`core/records.py`) atomically *before* the rename, so a
   crash can never leave a final output without its record;
3. rename the temp into place and confirm the placed file by size + sampled digest;
4. delete the source only when the job asked for it: one atomic unlink, only after the placed
   output validated and was recorded, and only while the source still matches the fingerprint
   captured at claim (`delete_source_verified`).

The record ties the output to the clip identity (`root_id` + archive-relative path + file name)
and to the source fingerprint captured at claim. An existing destination file is **skipped**
only when its record names the clip, the source fingerprint still confirms the claim, the file
still matches the recorded output fingerprint, and the full validation passes; anything else
that occupies the destination path is a **collision** (review, never overwritten). Same-folder
jobs keep the `manifest.json` marker above; destination placements record a sidecar instead,
because the marker's fingerprint is compared against the file at the clip's own path (see
"Archive scan artifacts"). A custom folder must already exist on the node; registered output
roots resolve through the node's `[roots]` mapping and may create the relative sub-path.

Invariant: if a source file is absent, a recorded, validated destination output for that clip
exists. Crash points — kill during the scratch copy/encode, destination staging/validation, the
record write, the rename, or the delete — leave either (source present, no final output or an
output to re-classify) or (source absent, recorded validated output). A resume reaps temps and
stale records idempotently (`cleanup_destination`) and never adopts a leftover temp; a missing
source with a matching record settles the job as skipped instead of review.

## Controller HTTP contract v1

The controller serves HTTP/1.1 with JSON payloads. The GUI hosts it in-process; workers reach it
over the local network.

- Base URL: `http://<bind>:<port>/api/v1`, default bind `127.0.0.1:8765`. Bind address and port
  are configurable (issue #7); use the LAN interface (e.g. `0.0.0.0:8765`) when workers run on
  other machines. The token is required in every deployment. The `[controller]` section of the
  per-user config holds `host`, `port`, and `token`; `vc controller` flags override the bind
  address for one run.
- Auth: every request carries `Authorization: Bearer <token>`. The token is generated once and
  stored in the per-user config; workers receive it out of band and it is never committed.
  Missing or invalid token → `401`.
- Conventions: jobs are identified by `root_id` + `relative_path` (archive-root-relative POSIX
  path). Worker ids match `[A-Za-z0-9_.-]{1,64}`. Direct-conversion jobs (issue #39) additionally
  carry a destination spec and a `delete_original` flag; their identity stays the pair.
- Errors: `{"error": {"code": "<slug>", "message": "<human text>"}}` with `400` malformed body,
  `401` auth failure, `404` unknown worker, `409` lease conflict.

### Lease semantics

- A successful claim starts a 90 s lease (`LEASE_TTL_SECONDS = 90`).
- Workers renew through heartbeat every 15 s (`HEARTBEAT_INTERVAL_SECONDS = 15`).
- No renewal within 90 s → the lease is expired: the worker counts as offline, its active job is
  recoverable, and the controller runs reconciliation (issue #5) before the clip can be claimed
  again. Nothing may be claimed twice while a live lease or unresolved recovery exists.
- Stop instructs the worker to terminate the current FFmpeg process; states stay durable and a
  later resume requeues the clip.

### Endpoints

#### `POST /api/v1/workers/register`

Register a worker and its archive-root mappings. Response carries the lease and heartbeat
intervals.

```json
// request
{
  "worker_id": "homeserver",
  "name": "Linux homeserver",
  "roots": {"archive": "/srv/archive"},
  "scratch_dir": "/srv/scratch",
  "thread_cap": 2,
  "version": "0.1.0"
}
// response 200
{"lease_seconds": 90, "heartbeat_seconds": 15}
```

#### `POST /api/v1/workers/{worker_id}/heartbeat`

Renews the worker lease and, when `active` is present, the job lease; carries progress for the
GUI. `"stop": true` in the response tells the worker to terminate the active job.

```json
// request
{
  "active": {"root_id": "archive", "relative_path": "2024-06-Ride/GS010001.MP4"},
  "progress": {
    "phase": "encoding",
    "pass": 2,
    "percent": 41.2,
    "fps": 38.5,
    "speed": "0.74x",
    "eta_seconds": 1234
  }
}
// response 200
{"stop": false, "lease_seconds": 90}
```

The optional `progress.phase` (`copied`, `encoding`, `validated`, `transferring`, `replacing`)
advances the clip's manifest stage; a heartbeat carrying `active` for a clip the worker no longer
holds is rejected with `409` (the worker must abort and discard its local work).

#### `POST /api/v1/jobs/claim`

Claim the next queued clip for a worker. `204` when nothing is available. Claiming is
transactional: two workers racing can never both win one clip. The controller fills the template's
effective settings into the job; every clip carries the same video bitrate.

```json
// request
{"worker_id": "homeserver"}
// response 200
{
  "job": {
    "root_id": "archive",
    "relative_path": "2024-06-Ride/GS010001.MP4",
    "template_id": "motorcycle",
    "settings": {
      "output_height": 720,
      "video_bitrate_kbps": 800,
      "audio_bitrate_kbps": 96,
      "ffmpeg_preset": "medium"
    },
    "settings_hash": "<hash of effective settings>",
    "capture_time": "2024-06-14T17:41:00+08:00",
    "destination": {"kind": "output_root", "root_id": "compressed", "relative_path": "2026-10"},
    "delete_original": false
  }
}
```

`destination` and `delete_original` (issue #39) carry the direct-conversion job spec for
enqueued convert jobs; legacy or script-seeded clips omit both fields and keep the
replace-at-source flow. The destination object carries `kind` plus, for an output root,
`root_id` and an optional `relative_path`. Worker clients ignore fields they do not use, so the
addition stays backwards compatible.

#### `POST /api/v1/jobs/complete`

Final replacement is done and validated at the source path. `204` on success; `409` when the
caller no longer holds the lease.

```json
// request
{
  "worker_id": "homeserver",
  "root_id": "archive",
  "relative_path": "2024-06-Ride/GS010001.MP4",
  "output": {"size_bytes": 48291034, "mtime_ns": 1750000000000000000, "digest": "<hash>"}
}
```

#### `POST /api/v1/jobs/fail`

Report a terminal failure. `"disposition": "review"` parks the clip in the review queue (for
example, when the source changed since claim); `"failed"` marks it failed; `"requeue"` returns
the clip to the queue (used to acknowledge an operator stop). `204` on success.

```json
// request
{
  "worker_id": "homeserver",
  "root_id": "archive",
  "relative_path": "2024-06-Ride/GS010001.MP4",
  "error": "source fingerprint changed before replacement",
  "disposition": "review"
}
```

#### `POST /api/v1/jobs`

Batch enqueue of explicit files for direct conversions (issue #39; the convert flow of epic #38).
The caller expands directories and filters media; the controller never walks the filesystem.
Every accepted file becomes a durable `queued` job that the existing workers claim through
`POST /api/v1/jobs/claim`.

```json
// request
{
  "files": [
    {"root_id": "archive", "relative_path": "VIDEOS/MOTORCYCLE/GS010001.MP4"},
    {"root_id": "archive", "relative_path": "IMPORTS/GOPRO-20261010/GH011016.MP4"}
  ],
  "destination": {"kind": "output_root", "root_id": "compressed", "relative_path": "2026-10"},
  "template_id": "motorcycle",
  "delete_original": false
}
// response 200
{
  "jobs": [
    {"job_id": "archive:VIDEOS/MOTORCYCLE/GS010001.MP4",
     "root_id": "archive", "relative_path": "VIDEOS/MOTORCYCLE/GS010001.MP4",
     "status": "queued", "reason": null},
    {"job_id": "archive:IMPORTS/GOPRO-20261010/GH011016.MP4",
     "root_id": "archive", "relative_path": "IMPORTS/GOPRO-20261010/GH011016.MP4",
     "status": "skipped", "reason": "already_complete"}
  ],
  "queued": 1, "skipped": 1, "rejected": 0
}
```

- `files` — required, non-empty list of explicit files as `root_id` + archive-relative POSIX
  path. A directory entry is rejected (`not_a_file`), never expanded here.
- `destination` — required destination spec (below).
- `template_id` — optional; defaults to the default template; must be configured.
- `delete_original` — optional boolean; default `false` (keep originals). A `same_folder`
  destination replaces the source, so it requires `delete_original: true`.

Per-file `status`/`reason`:

| status | reason | meaning |
| --- | --- | --- |
| `queued` | `null` | a new `queued` record was created |
| `skipped` | `already_queued` | a queued or in-flight record exists (idempotent re-enqueue) |
| `skipped` | `already_complete` | a completed record exists (never re-encode silently) |
| `skipped` | `needs_review` | the record is parked in review |
| `skipped` | `previously_failed` | the record failed before; retry is a follow-up |
| `skipped` | `duplicate_in_batch` | the same pair is repeated in this request |
| `rejected` | `unknown_root` | the source `root_id` is not registered on the controller |
| `rejected` | `unsafe_relative_path` | absolute, `..`, empty-segment, or escaping path |
| `rejected` | `not_a_file` | the resolved path is missing or not a regular file |
| `rejected` | `unreadable` | the file exists but fingerprinting failed |

The batch-level fields are validated before anything is written, so a malformed request
(`400 bad_request`) writes nothing. Re-enqueue is idempotent: an existing record is never
rewritten by an enqueue. `job_id` is informational (`<root_id>:<relative_path>`); the durable
identity stays the pair. Each request logs one `info` event with queued/skipped/rejected counts.

Destination specs name the output **folder** only; the output filename and collision handling
belong to the worker pipeline (#40).

| Spec | Meaning |
| --- | --- |
| `{"kind": "same_folder"}` | the verified swap: the output replaces the source at its own path; requires `delete_original: true` |
| `{"kind": "output_root", "root_id": "<id>", "relative_path": "<folder>"}` | a registered output root plus a root-relative POSIX folder; an omitted `relative_path` mirrors the source's archive-relative path |

The worker also decodes a node-local `custom_folder` spec
(`{"kind": "custom_folder", "path": "<absolute folder>"}`, issue #40); this endpoint does not
produce it (candidate follow-up). The operator's "custom folder" choice (epic #38) is expressed
as an `output_root` destination: the GUI convert flow (issue #41,
`gui/convert_service.py`) resolves the picked folder through the registered roots
(`core.config.root_for_folder`) and sends `root_id` + relative path. `output_root` sources and
destinations never carry host paths; every node resolves `root_id` + `relative_path` through its
own worker `[roots]` mapping, so the Mac's mounted path and the homeserver's local path run the
same job. `delete_original` records the operator's choice; the pipeline may delete a source only
after its output exists at the destination and validates (#40), and a `same_folder` job always
consumes the source through the verified swap.

#### `POST /api/v1/workers/{worker_id}/stop`

Request that the worker terminate its active job (same effect as the heartbeat `stop` flag).
`204`.

#### `GET /api/v1/events?after=<sequence>`

Run-log events for the GUI log view and per-run summaries. `after` is optional; without it the
most recent events (bounded) are returned.

```json
// response 200
{
  "events": [
    {"seq": 41, "at": "2026-10-10T14:02:11+08:00", "level": "info", "message": "claimed GS010001.MP4"}
  ],
  "next": 42
}
```

#### Additive endpoints (issue #7)

Monitoring and stop/resume additions; every v1 payload above stays valid.

- `POST /api/v1/workers/{worker_id}/resume` — clears a pending stop request. `204`.
- `GET /api/v1/workers` — registry snapshot for monitors: worker id, name, online, last seen,
  root mappings, stop flag, active clip, and the latest heartbeat progress.
- `GET /api/v1/summary?run_id=<id>` — run summary counts (completed, skipped, failed,
  review-needed), from a finished run or computed live.

#### Lease sweep and reconciliation

The controller sweeps every 5 s: workers silent for 90 s flip to offline, and expired job leases
are recovered through `Manifest.reconcile()`. Clips that expired in the copy/encode stages are
requeued; clips in the replacement stages (`validated`, `transferring`, `replacing`) park in
`review` until the issue #5 recovery protocol confirms the source/backup state. Nothing is
claimable twice while a live lease or unresolved recovery exists.

Payloads are intentionally minimal: adding optional fields is backwards compatible; removing or
renaming a field requires contract v2.

## Queue storage and migration (issue #39)

The durable queue is the SQLite manifest (`core/manifest.py`); destination specs and the
`delete_original` flag live on the job row itself (`destination_json`, `delete_original`), so a
job's identity, lease state, and conversion spec never drift apart. `core/manifest_schema.py`
owns the clips/events DDL, the row codecs, and an idempotent migration that adds the conversion
columns the first time an older database is opened writable. A read-only open never migrates and
tolerates the missing columns, so a dry `vc scan` keeps working before the controller upgrades;
old code can still read a migrated database because the added columns are ignored.
