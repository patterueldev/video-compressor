# Video Compressor

## Purpose

Build a small GUI app for compressing GoPro motorcycle footage — the existing archive and later
footage imports — into compact H.265 copies. The primary flow is direct: hand the app videos
(drag & drop or pick them), choose where the outputs go, decide whether originals are deleted
after a verified conversion, and let the registered worker nodes share the work.

Keep the processing engine separate from the GUI so a later interface change does not affect
probing, encoding, validation, or resume behavior. Archive-wide scanning and dry runs remain
available as optional tooling (see "Optional scan and dry-run tooling").

## V1 workflow (convert flow)

1. Start the app (`vc gui`). The GUI process hosts the controller and owns the durable queue, or
   attaches to a controller already serving the configured address.
2. Select the videos in the Convert tab: drag files and/or folders into the app, or use the
   open-files / open-folder dialogs. A dropped folder recurses and picks up video files
   (`.mp4`/`.mov`) only; a folder outside the registered roots is registered as a new root
   automatically, and files already under a root keep resolving to it. Selections append to a
   pending list showing the clip count and total size.
3. Choose the destination: a registered output root (optionally with a subfolder), a custom
   folder (registered as a new output root on use when it is outside the registered roots), or
   "same folder as source" — the verified swap that replaces each source at its own path and
   therefore consumes the original.
4. Decide whether originals are deleted after conversion. The default is to keep them; a source
   is deleted only after its output exists at the destination and passes validation.
5. Choose the processing template; its settings (single video bitrate, output height, audio,
   preset) apply to every selected clip.
6. Press Start. Each file becomes a durable job in the controller's queue and is distributed
   across the registered worker nodes; each node processes at most one clip at a time, and the
   controller prevents two nodes from claiming the same clip.
7. Watch the job monitor: per-node progress, encoding speed, estimated remaining time, and the
   run log. Stopping is safe and resuming later is supported. Re-running the same selection is
   safe: clips already queued or completed are skipped.

The GUI process is the controller and owns the durable queue and manifest while a run is active.
Registered worker nodes pull jobs from it over the local network. The controller must remain
available during processing; if it disconnects, workers stop claiming jobs and active jobs become
recoverable. A job includes the source-relative path, the conversion settings, the destination
spec, and the delete-original choice, plus a lease that makes it recoverable. Each node resolves
paths through its configured root mapping, so the same relative clip path resolves correctly on
the Mac, another computer, or the Linux homeserver.

Use the mounted SMB path as the drag-and-drop interface on the Mac. Each worker uses the fastest
reliable route it has to the archive; the mounted/local-path decision is recorded in
`docs/pilot-notes.md`.

## Optional scan and dry-run tooling

`vc scan` and `vc probe` (and the GUI Library tab) remain available for archive-wide checks and
reporting; a dry run never modifies the archive. The Library tab also lists the registered
roots, which can be removed again while they have no non-terminal clips; removal keeps manifest
history and leaves remote node configs alone. The scan classifies each clip as new, complete,
changed, or review, and shows capture time, bucket, bitrate, and estimated output size. Those
labels feed the dry-run report and the GUI tables only — the convert flow does not require a scan,
and conversion eligibility comes from the explicit selection, the job's destination, and the
manifest.

## Templates and settings

Make encoding values editable in the app and save them in a human-readable per-user configuration
file. Support named templates, with only the motorcycle template included in v1; leave room to add
vlog settings later.

Initial motorcycle defaults:

| Setting | Initial value |
|---|---:|
| Output height | 720 pixels |
| Video bitrate | 800 kbps |
| Timezone | `Asia/Manila` |
| Audio | AAC, 96 kbps |
| FFmpeg preset | `medium` |
| Original handling | Keep originals; replace or delete only after a validated conversion |

One video bitrate applies to every clip. The operator set 800 kbps for all buckets (daytime,
evening, dim, motion-heavy) rather than adjusting an evening value after a candidate-bitrate
comparison; the decision is recorded in `docs/pilot/settings-review.md` and the value remains
editable per template. The app should estimate output size from clip duration and configured video
plus audio bitrates. For reference, 800 kbps video alone is about 90 MB for 15 minutes; AAC audio
and container overhead add to that, so actual size depends on duration and settings.

### Output names

Each template carries an output name pattern applied when the destination is an output folder
(a registered output root or a custom folder): literal text plus capture-time tokens. The tokens
are `{name}` (the source clip's file name without its suffix), `{date}`, `{time}`, and
`{datetime}`; the date/time tokens accept an optional `:strftime` format inside the braces
(for example `{date:%Y%m%d%H%M}_{name}`). The default `{name}` keeps the source clip's own file
name, so existing setups behave exactly as before. The pattern decides the output file name only:
same-folder swaps always keep the source name, and sources as well as already-placed outputs are
never renamed by it. A pattern that needs a capture time fails that clip visibly when none can be
found, rather than writing a wrongly named output.

## Capture time and classification

These rules feed the optional scan/dry-run tooling, the review queue, and the GUI tables; the
convert flow processes the files the operator selected.

- Use capture time from video metadata, never filesystem modification time. Probe files with
  `ffprobe`; show unreadable or unsupported files as skipped with a reason.
- Convert timestamps with an explicit timezone offset into the configured timezone. Interpret
  timestamps without an offset as local wall-clock time in that timezone.
- Label each clip daytime before 17:30 and evening/night from 17:30 onward; the label feeds the
  dry-run report only, and the single configured video bitrate applies to both.
- Send missing or conflicting timestamps to a review queue and do not encode those files.
- The timezone interpretation was confirmed against known clips and GPS data; see
  `docs/pilot-notes.md` for the evidence and the `local_wall_clock` decision.

## Processing, replacement, and completion tracking

- A worker copies its claimed source clip from the network archive to local scratch storage, then
  probes and encodes locally. Require enough scratch space for the source, temporary output, and
  pass logs before starting the job.
- Process one clip at a time per worker with FFmpeg using two-pass `libx265`, the selected average
  video bitrate, and configured preset. Multiple registered nodes may process different clips in
  parallel.
- Map the primary video stream and available audio stream(s); encode audio as AAC at the
  configured bitrate. Exclude telemetry/data and other non-audio/video streams by default.
- Validate the local output before delivery, then deliver it according to the job's destination:
  - **Same folder as source** (the verified swap): copy the output to a unique temporary file
    beside the source on the archive filesystem, verify the transfer, stage the original as a
    recoverable backup, atomically rename the temporary output into the original path, confirm
    the replacement is readable, then delete the backup.
  - **A registered output root or custom folder** (destination placement): stage the output
    beside the destination file with the same directory, verify it by full read-back, validate it
    at the destination, write the completion record (`.vc-records/<name>.vc-meta.json` inside the
    destination folder) before the rename, rename the temporary file into place, and confirm the
    placed file. The source is deleted only when the job asked for it — one atomic delete, after
    the placed output validated and was recorded.
  - If any step fails, preserve the original or backup so the source can be restored.
- Set each converted output's filesystem creation date from the clip's embedded capture time
  (app-side on macOS, after the batch or through the Library fix-up action / `vc fix-dates`);
  the modification date stays the conversion time. No automatic path stamps originals: only the
  batch's output root is swept for outputs written during that batch.
- Before any replacement or deletion, confirm the source still matches the fingerprint captured
  when the job was claimed. If another process changed it, do not overwrite or delete it; return
  the job to review.
- An existing destination file is skipped only when its completion record names this clip, the
  source fingerprint still confirms the claim, the file still matches the recorded output
  fingerprint, and validation passes. Records live in a hidden `.vc-records/` folder inside the
  destination directory; a record written beside the output by an older version is still honored
  as a fallback. Anything else occupying the destination path is a collision
  and goes to review — never a silent overwrite.
- Remove local scratch files and pass logs after a successful conversion. Discard incomplete local
  encodes on the next run and restart that clip from the beginning.
- Maintain a central manifest with source-relative path, original source size and modification
  time, selected capture time and settings, destination and delete-original choice, assigned
  node, status, output fingerprint, and completion time. Track stages such as queued, claimed,
  copied locally, encoding, validated, transferring, replacing, complete, failed, and
  review-needed.
- Mark a clip complete only when its output exists at the destination and passes validation, and
  the manifest matches the effective settings and output fingerprint. Reruns skip valid completed
  clips, including when the template later changes; new settings apply to new footage.
  Reprocessing a completed clip requires an explicit user action. Changed source fingerprints
  queue the clip for review rather than silently replacing it.
- Give each claimed job a lease so the controller can recover it if a worker disappears. On
  restart or lease expiry, reconcile the source, destination artifacts, temporary transfer, and
  manifest before retrying. Prevent a second worker from claiming that clip during recovery.
- On stop or interruption, terminate the active FFmpeg process. Before delivery begins, leave the
  source untouched; during replacement, preserve enough state to restore the source. Keep
  successful clips complete when the controller or another node restarts.

## Validation and source safety

Validate local and transferred outputs with `ffprobe`: readable container, H.265/HEVC video,
expected 720-pixel height and proportional dimensions, duration close to the source, and audio
present if the source had audio. Validate at the destination before deleting a source. Keep a
recoverable backup until the replacement is confirmed readable at the original path; if the
filesystem cannot provide a safe same-filesystem rename, use a copy-and-verify replacement
procedure with recovery state. Temporary artifacts (`.vc-tmp.<job_id>`, `.vc-backup.<job_id>`,
and the `.vc-records/` `.vc-meta.json` records) are named per attempt so concurrent jobs cannot
collide and recovery can prove the source or output state. Each run gets a separate log and a
summary of completed,
skipped, failed, and review-needed clips.

## Acceptance

The pilot ceremony of issue #12 (pilot encodes, settings sweep, watched first replacement) is
superseded by the convert flow. The settings decision stands: one video bitrate, 800 kbps,
configurable (`docs/pilot/settings-review.md`); a candidate-bitrate sweep is not required. The
epic's acceptance is one real end-to-end run (issue #44):

1. Convert a small real folder of clips with delete-originals **off** to a destination folder;
   check the outputs decode, the sizes are sane, and every source is untouched.
2. Repeat on a copy of the folder with delete-originals **on**; check each source disappears only
   after its output validates.
3. Confirm both nodes (Mac and homeserver) engage, each processing one clip at a time, and record
   sanitized evidence.

Keep the per-node transport decision from `docs/pilot-notes.md` (mounted/local paths) and the
rerun expectations: completed clips skip after a template change, and changed sources go to
review. The archive-wide dry runs and the rerun matrix remain available as optional checks.

V1 is ready when the operator can drag or pick videos, choose a destination, optionally have
verified originals deleted, watch the run across one or more workers, and re-run without redoing
completed clips or losing a source.
