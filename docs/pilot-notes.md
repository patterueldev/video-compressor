# Pilot dry run — real folders, classification, and capture-time interpretation

**Issue:** [#11](https://github.com/patterueldev/video-compressor/issues/11) (plan.md pilot steps 1–2)
· **Date:** 2026-10-10 · **Operator:** Pat
**Tool:** `vc` 0.1.0 at commit `e4fe843`; venv (Python 3.14.8), ffprobe/ffmpeg 8.1.1 (Homebrew).

All scans were read-only: the tool wrote only the report JSON files below; nothing on the share
was modified.

## Scope and setup

- Archive root: `/Volumes/IronWolfHDD8TB/CAMERA` (SMB share `//pat@saturday.local/IronWolfHDD8TB`),
  pinned `--root-id camera`.
- No per-user config or manifest existed, so the scans used the shipped defaults: `Asia/Manila`,
  evening from 17:30, 800 kbps day and evening, AAC 96 kbps, preset `medium`, 720p,
  replace-after-validated-transfer, and `local_wall_clock` capture-time mode (the default under
  review). Settings hash `d7a81a5338b6f6a6`.
- Folders dry-run (operator choice):
  1. `VIDEOS/MOTORCYCLE` — the existing motorcycle archive; contains the rides near the 17:30
     boundary.
  2. `IMPORTS/GOPRO-20261010` — the newest GoPro import (2026-10-10).
  3. `IMPORTS/DJI_BACKUP` — outside the GoPro set (DJI action camera).
- Not scanned: the older GoPro import `IMPORTS/GOPRO/0104-2026` and the known wrong-date batch
  `IMPORTS/GOPRO ~03012026 (wrong dates)`; both are covered by the anomaly notes below.

## Dry-run results

| Folder | Media found | Classification | Estimated output | Report |
| --- | --: | --- | --: | --- |
| `VIDEOS/MOTORCYCLE` | 129 MP4 | 129 new | 8,210,243,464 B (≈8.2 GB) | [motorcycle-archive.json](pilot/reports/motorcycle-archive.json) |
| `IMPORTS/GOPRO-20261010` | 68 MP4 | 68 new | 4,644,728,269 B (≈4.6 GB) | [gopro-20261010.json](pilot/reports/gopro-20261010.json) |
| `IMPORTS/DJI_BACKUP` | 28 MP4 | 28 new | 774,897,049 B (≈0.8 GB) | [dji-backup.json](pilot/reports/dji-backup.json) |

- No `review` or `changed` rows anywhere. Two informational notes: `202505261800_GH010763.MP4`
  and `IMPORTS/GOPRO-20261010/GH011016.MP4` have no audio stream (short/silent clips).
- Non-media files are ignored by design: `.bzEmpty`, JPGs, `.THM`/`.LRV`, and DJI's 28 `.lrf`,
  18 `.jpg`, 14 `.dng` companions.
- The reports are committed as generated (absolute archive root included). The scans were re-run
  after this branch fast-forwarded to `e4fe843`; outputs are identical apart from `generated_at`.

## Capture-time interpretation — decision

**Decision: keep the `local_wall_clock` default. Do not switch to `utc`.**

This GoPro (HERO9 Black) writes *local wall-clock time* into `creation_time` and attaches a
misleading `Z` suffix. Two independent in-file sources confirm this across 2025–2026:

- **GPMF telemetry (video):** the `GPSU` record is satellite UTC; `creation_time` face = GPSU + 8 h.
- **EXIF (photos):** `DateTimeOriginal` (local) = GPS `GPSTimeStamp` (UTC) + 8 h, and matches the
  `creation_time` of clips from the same session.

| Evidence | `creation_time` face | in-file GPS (UTC) | +8 h → local | clock drift |
| --- | --- | --- | --- | --- |
| `VIDEOS/MOTORCYCLE/202504271820_GX010750.MP4` | 2025-04-27 10:20:37Z | 02:16:38Z | 10:16:38 | +4 min |
| `VIDEOS/MOTORCYCLE/202505261800_GH010763.MP4` | 2025-05-26 10:00:12Z | 01:59:09Z | 09:59:09 | +1 min |
| `VIDEOS/MOTORCYCLE/20250924013510_GX010817.MP4` (boundary) | 2025-09-23 17:35:10Z | 09:23:58Z | 17:23:58 | +11 min |
| `IMPORTS/GOPRO-20261010/GH011016.MP4` | 2026-09-07 16:14:03Z | 08:12:58Z | 16:12:58 | +1 min |
| JPG EXIF, 10 photos (2025-06-06/07/09, 2026-09-07/28) | local EXIF time | UTC GPS time | matches local | ≤1 min |

Counterfactual under `utc`: every capture time shifts +8 h and **96 of 129** MOTORCYCLE buckets
flip (45 daytime / 84 evening, versus the correct 117 / 12); the boundary ride above would read
01:35 instead of 17:35. `utc` is decisively wrong for this footage.

Boundary/near-boundary rides (resolved local times): `20250924013510_GX010817.MP4` 17:35:10 →
evening (GPS-true 17:23:58); `20250924010324_GX010816.MP4` 17:03:24 → daytime; newest import:
`GX011013` 17:47:34, `GX011031` 17:45:10, `GX011029` 17:51:14 → evening; `GX011021` 17:12:26 →
daytime.

Operator check: the exact ride times could not be recalled from memory, so the in-file,
satellite-derived evidence is treated as authoritative for this decision. Camera-clock drift of
+1…11 min against GPS was observed; decisions within ~15 min of 17:30 can therefore flip with the
camera clock. That is acceptable for now because day and evening bitrates are identical
(800 kbps), and the evening start remains configurable.

## Classification anomalies

1. **MOTORCYCLE filename prefixes run 8 h ahead of the actual capture time.** Examples:
   `202505261800_GH010763.MP4` was recorded ≈09:59 (not 18:00); `202504271820_GX010750.MP4` ≈10:16
   (not 18:20); `20250924013510_GX010817.MP4` ≈17:24 (not 01:35). The June 2025 JPG prefixes are
   correct (e.g. `20250606134242_GOPR0782.JPG` matches its EXIF to the second), so an import step
   double-shifted the MP4 names — consistent with treating the local-with-`Z` timestamp as UTC.
   The compressor reads metadata only, so processing is unaffected; fixing archive naming is an
   operator decision outside this pilot.
2. **Wrong-date batch** (`IMPORTS/GOPRO ~03012026 (wrong dates)`): dead-battery camera-clock reset
   (operator-confirmed). Example: `GOPR0860.JPG` EXIF says 2016:02:20 19:54 while GPS says
   2026-02-08 05:06:41Z → 13:06:41 local. Files with a reset clock parse as plausible dates and
   will **not** auto-flag for review; they need operator review when that batch is processed.
   The same reset pattern appears in `IMPORTS/GOPRO/0104-2026` JPGs (2016:01:24).
3. **DJI writes true UTC**, unlike GoPro: e.g. `DJI_20260129111821_0001_D.MP4` face
   `2026-01-29T03:18:22Z` = 11:18:22 local (camera filenames are local time). Under
   `local_wall_clock`, DJI capture times display 8 h early. Candidate follow-up for per-vendor
   capture-time handling when the DJI footage joins a processing run; the GoPro motorcycle flow
   is unaffected.
4. Chapter sets (`GX02…`, `GX03…` of one recording) share the first chapter's capture time —
   expected; each chapter is classified and size-estimated separately.

## Access-route observations (input for the transport decision, #13)

Route at measurement time: `//pat@saturday.local/IronWolfHDD8TB` → `/Volumes/IronWolfHDD8TB`;
SMB 3.1.1, signing AES_128_CMAC on, encryption off. The Mac (192.168.254.105) and `saturday`
(192.168.254.128) sit on the same LAN, but macOS routed the session via `utun21` (Tailscale,
100.113.8.56); RTT ≈ 5 ms.

Measured on `202504271820_GX010750.MP4` (4,005,605,553 B):

| Operation | Time | Throughput |
| --- | --: | --: |
| SMB sequential read, pass 1 | 46.43 s | 86.3 MB/s |
| SMB sequential read, pass 2 | 46.30 s | 86.5 MB/s |
| SMB → local scratch copy (simulates `fetch_source`) | 68.31 s | 58.6 MB/s |

- Scan cost: the 129-clip archive (~500 GB) scans in ≈1.5 min cold (2 MB sampled fingerprint +
  ffprobe header per clip = 129 reads), 35 s with client caches warm — dominated by per-file
  round trips, not bandwidth.
- Caveats: single session; scratch target was the boot SSD (93 % full); no SSH/rsync or
  homeserver-local comparison was measured on this Mac (that comparison belongs to #13).

## Follow-ups

- [ ] (candidate) Per-vendor capture-time behaviour (GoPro local-with-`Z` vs DJI true UTC) — file
      an issue when DJI footage joins a processing run.
- [ ] (optional, data-only) Repair or accept the +8 h MOTORCYCLE filename prefixes outside this
      pilot.
- `local_wall_clock` default: **confirmed unchanged** (this pilot's timezone decision).

## Reproduction

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'

.venv/bin/vc scan --root /Volumes/IronWolfHDD8TB/CAMERA \
  --folder /Volumes/IronWolfHDD8TB/CAMERA/VIDEOS/MOTORCYCLE \
  --root-id camera --report docs/pilot/reports/motorcycle-archive.json
# (same for IMPORTS/GOPRO-20261010 and IMPORTS/DJI_BACKUP)

# GPS cross-check: extract the GoPro telemetry stream from the first 30 s of a clip and read the
# 16-byte GPSU record (YYMMDDHHMMSS.sss, UTC):
ffmpeg -v error -t 30 -i <clip.mp4> -map 0:d:1 -c copy -f data out.bin
```

Use `vc probe <clip>` to print one clip's raw `creation_time`, its source tag, the resolved time,
bucket, and review state.
