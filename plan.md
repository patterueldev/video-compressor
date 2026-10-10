# Video Compressor

## Purpose

Build a small GUI app for repeatedly importing and compressing GoPro motorcycle footage from the
homeserver share. It should work for the existing archive and for new footage folders added later.
The user should be able to drag a footage folder into the app, review what will be processed, and
start or resume a safe conversion.

Keep the processing engine separate from the GUI so a later interface change does not affect
probing, encoding, validation, or resume behavior.

## V1 workflow

1. Mount the SMB share in macOS and drag a GoPro footage folder into the app. Also provide a
   folder picker as an alternative. Repeat this for later footage folders.
2. Choose a processing template and review the selected archive root. Workers use a configured
   path mapping for that root, whether it is an SMB mount or a local filesystem path.
3. Scan recursively for `.mp4` and `.mov`. Use the manifest and current source fingerprints to
   show each clip as new, already complete, changed since processing, or needing review.
4. Show capture time, local timezone, selected bitrate, and planned replacement for each eligible
   clip. Let the user edit settings, inspect the queue, and start a dry run or processing job.
5. Show active jobs by node, recent encoding speed, progress, estimated remaining time, and run
   log. Allow stopping safely and resuming later.

The GUI process is the controller and owns the durable queue and manifest while a run is active.
Registered worker nodes pull jobs from it over the local network. The controller must remain
available during processing; if it disconnects, workers stop claiming jobs and active jobs become
recoverable. A job includes the archive-relative path and selected settings; each node resolves
the path through its configured archive-root mapping. Each node runs at most one FFmpeg conversion
at a time. The controller must prevent two nodes from claiming the same source clip concurrently.

Use the mounted SMB path as the drag-and-drop interface on the Mac. During the pilot, compare
representative source reads through SMB with SSH/local filesystem access where available. For
each worker, use the fastest reliable way it can access the archive; do not assume SSH is faster.
Keep each worker's archive-root mapping explicit so the same relative clip path resolves correctly
on the Mac, another computer, or the Linux homeserver.

## Templates and settings

Make encoding values editable in the app and save them in a human-readable per-user configuration
file. Support named templates, with only the motorcycle template included in v1; leave room to add
vlog settings later.

Initial motorcycle defaults:

| Setting | Initial value |
|---|---:|
| Output height | 720 pixels |
| Daytime video bitrate | 800 kbps |
| Evening/night video bitrate | 800 kbps |
| Evening starts | 17:30 |
| Timezone | `Asia/Manila` |
| Audio | AAC, 96 kbps |
| FFmpeg preset | `medium` |
| Original handling | Replace source after validated transfer |

Day and evening/night bitrates are independently configurable. The initial evening setting is
provisional; adjust it after personally checking pilot footage at candidate bitrates, especially
dim and motion-heavy clips. The app should estimate output size from clip duration and configured
video plus audio bitrates. For reference, 800 kbps video alone is about 90 MB for 15 minutes; AAC
audio and container overhead add to that, so actual size depends on duration and settings.

## Capture time and classification

- Use capture time from video metadata, never filesystem modification time. Probe files with
  `ffprobe`; show unreadable or unsupported files as skipped with a reason.
- Convert timestamps with an explicit timezone offset into the configured timezone. Interpret
  timestamps without an offset as local wall-clock time in that timezone.
- Apply the daytime bitrate before 17:30 and the evening/night bitrate from 17:30 onward.
- Send missing or conflicting timestamps to a review queue and do not encode those files.
- Before a full run, compare the app's detected capture times against several known clips and
  confirm the timezone interpretation.

## Processing, replacement, and completion tracking

- A worker copies its claimed source clip from the network archive to local scratch storage, then
  probes and encodes locally. Require enough scratch space for the source, temporary output, and
  pass logs before starting the job.
- Process one clip at a time per worker with FFmpeg using two-pass `libx265`, the selected average
  video bitrate, and configured preset. Multiple registered nodes may process different clips in
  parallel.
- Map the primary video stream and available audio stream(s); encode audio as AAC at the
  configured bitrate. Exclude telemetry/data and other non-audio/video streams by default.
- Validate the local output before sending it back. Copy it to a unique temporary file beside the
  source on the archive filesystem, verify the transfer, then stage the original as a recoverable
  backup and atomically rename the temporary output into the original path. Confirm the replacement
  is readable before deleting the backup. If any step fails, preserve the original or backup so
  the source can be restored.
- Before replacement, confirm the source still matches the fingerprint captured when the job was
  claimed. If another process changed it, do not overwrite it; return the job to review.
- Remove local scratch files and pass logs after successful replacement. Discard incomplete local
  encodes on the next run and restart that clip from the beginning.
- Maintain a central manifest with source-relative path, original source size and modification
  time, selected capture time and settings, assigned node, status, output fingerprint, and
  completion time. Track stages such as queued, claimed, copied locally, encoding, validated,
  transferring back, replacing, complete, failed, and review-needed.
- Mark a clip complete only when its replacement exists at the source path and passes validation,
  and the manifest matches the effective settings and replacement fingerprint. Reruns skip valid
  completed clips, including when the template later changes. New settings apply to new footage.
  Reprocessing a completed clip requires an explicit user action; after its original backup is
  deleted, reprocessing can only use the already-compressed replacement and may reduce quality.
  Changed source fingerprints queue the clip for review rather than silently replacing it.
- Give each claimed job a lease so the controller can recover it if a worker disappears. On
  restart or lease expiry, reconcile the source, backup, temporary transfer, and manifest before
  retrying. Prevent a second worker from claiming that clip during recovery.
- On stop or interruption, terminate the active FFmpeg process. Before replacement begins, leave
  the source untouched; during replacement, preserve enough state to restore the source. Keep
  successful clips complete when the controller or another node restarts.

## Validation and source safety

Validate local and transferred outputs with `ffprobe`: readable container, H.265/HEVC video,
expected 720-pixel height and proportional dimensions, duration close to the source, and audio
present if the source had audio. Verify the transferred file before swapping it into place. Keep a
recoverable backup until the replacement is confirmed readable at the original path; if the
filesystem cannot provide a safe same-filesystem rename, use a copy-and-verify replacement
procedure with recovery state. Each run gets a separate log and a summary of completed, skipped,
failed, and review-needed clips.

## Pilot and acceptance

1. Dry-run the selected SMB folder and inspect paths, capture times, classifications, and bitrate
   choices.
2. Compare SMB, SSH, and local filesystem access on representative large clips for each available
   worker; select and record the fastest reliable route per node.
3. Process pilot clips covering daytime, evening, motion-heavy, and dim footage into a separate
   pilot output area so the originals remain available while settings are reviewed.
4. Personally compare pilot detail with the source clips, especially signs and license plates, then
   adjust the motorcycle template bitrates.
5. Replace originals only after pilot settings are approved. Confirm local scratch encoding,
   verified transfer-back, recoverable replacement, and cleanup on one clip before enabling the
   full queue.
6. Add one more worker and confirm both nodes can process different clips concurrently, while
   each node runs only one conversion and no clip can be claimed twice.
7. Run the archive and future footage folders. Confirm reruns skip completed clips even after
   template changes, changed source files go to review, and failures recover without losing the
   source.

V1 is ready when the user can drag in SMB footage folders, review and configure the queue, use
one or more workers safely, replace validated sources, and resume processing across later footage
imports without redoing completed clips.
