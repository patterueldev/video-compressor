# First real replacement — watched runbook (issue #12)

> **Status: superseded.** The epic #38 convert flow replaced the #12 ceremony this runbook was
> written for; the one remaining real-clip checkpoint is the #44 acceptance run (delete off, then
> delete on a copy). The page is kept as an optional same-folder/recovery drill and as the
> historical evidence record — nothing below is a required gate anymore.

This is the watched first-replacement runbook from the superseded #12 ceremony: one real clip
replaced end-to-end with the operator watching — scratch encode → verified transfer → recoverable
swap → cleanup. It is written to be executed **after** the settings review; the sections at the
bottom are filled in as the evidence record of that run. Placeholders are used throughout: never
commit real share paths, hostnames, or tokens.

## Preconditions

- [ ] **CP-1 done:** `docs/pilot/settings-review.md` holds the operator's
      decision and the motorcycle template in the per-user config carries the
      approved daytime/evening bitrates.
- [ ] The archive share is mounted; `$ARCHIVE_ROOT` points at the mounted
      archive root and `$PILOT_ROOT` at the pilot output area.
- [ ] The worker config for this run has **no `[pilot]` section** (pilot mode is
      enabled only by a `[pilot] root`; without it the worker replaces). It
      maps the manifest's root id (`camera`) and points at a scratch directory
      with room for the source, output, and pass logs.
- [ ] `VC_WORKER_TOKEN` is exported (or the config uses its own token source);
      no token or path is written into the repository.
- [ ] The chosen clip has no stale swap artifacts (`.vc-tmp.*`/`.vc-backup.*`)
      and no other worker is running.
- [ ] The operator has chosen the clip, is present, and is watching.

Proposed clip (operator may swap for any small clip):

- `VIDEOS/MOTORCYCLE/20250720011645_GX030803.MP4` — ≈23 MB, ≈4.5 s, daytime.
- Alternates: `VIDEOS/MOTORCYCLE/20250923215557_GX040815.MP4` (≈75 MB, ≈13 s)
  or `IMPORTS/GOPRO-20261010/GX011029.MP4` (≈54 MB, ≈12 s, evening).

Chosen clip: `<to be recorded>`.

## Commands

The controller must not be running while the clip is seeded (SQLite writer
contention); start it afterwards. The queue is the per-user manifest, so this
run counts as a real job.

```sh
# 0. the approved settings must be live in the per-user config
.venv/bin/python -c \
  "from videocompressor.core.config import load_config; t=load_config().templates['motorcycle']; \
   print(t.video_bitrate_kbps)"

# 1. seed exactly the chosen clip (works for clips outside the pilot selection)
.venv/bin/python scripts/pilot_run.py enqueue \
  --selection docs/pilot/pilot-selection.toml \
  --clip "$CLIP_RELPATH" \
  --root "$ARCHIVE_ROOT" --root-id camera \
  --manifest "$MANIFEST_PATH"

# 2. start the controller with the per-user config (background terminal)
.venv/bin/vc controller

# 3. watched replacement (foreground; operator runs this or watches it)
VC_WORKER_TOKEN=… .venv/bin/vc worker --config "$WORKER_CONFIG" --once
```

What to watch in the worker log and the manifest states: `claimed` → `copied`
→ `encoding` → `validated` → `transferring` → `replacing` → `complete`. A stop
(Ctrl-C or SIGTERM) terminates the active encode and leaves the source
untouched; mid-swap a recoverable backup is preserved and the next claim
reconciles it (`core/recover.py`). The swap is a same-directory rename pair and
the backup is deleted **only after** the validator confirms the replacement is
readable at the original path.

## Post-checks (agent-run; record the results)

```sh
.venv/bin/vc probe "$ARCHIVE_ROOT/$CLIP_RELPATH"          # HEVC, 720p, audio per source

# manifest complete + output fingerprint matches; no swap artifacts remain
.venv/bin/python - "$ARCHIVE_ROOT" "$CLIP_RELPATH" "$MANIFEST_PATH" <<'PY'
import sys
from pathlib import Path

from videocompressor.core.fingerprint import fingerprint_file
from videocompressor.core.manifest import Manifest
from videocompressor.core.replace import find_artifacts

root, relative, manifest_path = sys.argv[1], sys.argv[2], sys.argv[3]
source = Path(root) / relative
with Manifest.open(Path(manifest_path), create=False) as manifest:
    record = manifest.get_clip("camera", relative)
print("state:", record.state)
print("output fingerprint matches:", record.output == fingerprint_file(source))
print("swap artifacts:", find_artifacts(source).all)
PY
```

Checklist:

- [ ] `vc probe` reports H.265/HEVC, 720-pixel height, audio present if the
      source had audio, duration close to the source.
- [ ] Manifest record is `complete` with `output` equal to the current
      fingerprint of the source path.
- [ ] The folder marker contains the clip (`core/markers.read_marker`).
- [ ] `find_artifacts(source).all == ()` — no `.vc-tmp.*` / `.vc-backup.*`.
- [ ] The source file size dropped to the expected encoded size (record
      before/after bytes).
- [ ] The replaced clip plays end-to-end without errors.

## Evidence

| Field | Value |
| --- | --- |
| Clip (archive-relative) | |
| Settings used (video kbps) | |
| Source size before → after | |
| Encode time (pass 1 / pass 2) | |
| Validation failures | |
| Swap artifacts after | none |
| Operator confirmation | |

Failures follow plan.md: keep the original or backup, park the clip in review,
and do not delete anything until `core/recover.reconcile_source` reports a
confirmed state.
