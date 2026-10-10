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
| `videocompressor.gui` | PySide6 app; a thin client that also hosts the controller in-process. | `core`, `controller` |
| `videocompressor.cli` | `vc` entry points wiring the packages together. | all of the above (PySide6 import stays lazy) |

Rules:

- `core` must never import `controller`, `worker`, or `gui` — the engine must outlive the GUI
  (plan.md hard requirement).
- Only `gui` imports PySide6, and PySide6 is installed only through the `gui` extra; CI and the
  worker never need it.
- CLI subcommands are stubs until their owning issue lands; each exits with code 2 and a clear
  "not implemented" message.

## Data flow

dropped folder → scan + classify (#2, #3) → queue (manifest) → worker claims a job (#7) → copy to
local scratch → two-pass x265 encode (#4) → validate (#4) → transfer back + replace (#5) → mark
complete in the manifest (#3) → run summary (#9)

The GUI process is the controller and owns the durable queue and manifest while a run is active.
Workers pull jobs and resolve each job's archive-relative path through their own configured
archive-root mapping, so the same relative path works on the Mac, another computer, or the Linux
homeserver.

## Access-transport seam

Everything above the seam works with local file paths only. The worker pipeline reaches the
archive through three primitive operations, implemented against the worker's resolved local path:

| Primitive | Contract |
| --- | --- |
| `fetch_source(relative_path, scratch_dir)` | Copy the source clip into local scratch and verify the copy. |
| `put_back(local_output, relative_path)` | Copy the encoded output to a unique temp file beside the source on the archive filesystem; verify the transfer by read-back. |
| `replace(temp_path, relative_path)` | Fingerprint pre-check → rename source to a recoverable backup → atomic rename temp into place → confirm readable → delete backup. |

v1 implements these directly on the worker-local filesystem: the SMB mount on macOS, the local
ext4 path on the Linux homeserver. A later access-transport decision (issue #13) can add SSH/rsync
transports behind the same three primitives without touching scan, encode, or replacement logic.
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

## Controller HTTP contract v1

The controller serves HTTP/1.1 with JSON payloads. The GUI hosts it in-process; workers reach it
over the local network.

- Base URL: `http://<bind>:<port>/api/v1`, default bind `127.0.0.1:8765`. Bind address and port
  are configurable (issue #7); use the LAN interface (e.g. `0.0.0.0:8765`) when workers run on
  other machines. The token is required in every deployment.
- Auth: every request carries `Authorization: Bearer <token>`. The token is generated once and
  stored in the per-user config; workers receive it out of band and it is never committed.
  Missing or invalid token → `401`.
- Conventions: jobs are identified by `root_id` + `relative_path` (archive-root-relative POSIX
  path). Worker ids match `[A-Za-z0-9_.-]{1,64}`.
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

#### `POST /api/v1/jobs/claim`

Claim the next queued clip for a worker. `204` when nothing is available. Claiming is
transactional: two workers racing can never both win one clip. The controller resolves the
day/evening bucket per clip, so the job carries the effective video bitrate.

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
    "capture_time": "2024-06-14T17:41:00+08:00"
  }
}
```

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
example, when the source changed since claim); `"failed"` marks it failed. `204` on success.

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

Payloads are intentionally minimal: adding optional fields is backwards compatible; removing or
renaming a field requires contract v2.
