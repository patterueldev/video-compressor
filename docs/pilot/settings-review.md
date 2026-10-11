# Pilot settings review — motorcycle template (issue #12)

Status: **CP-1 decided by the operator** — one bitrate for all buckets; the candidate-bitrate
sweep is not required.

> **Superseded ceremony:** the epic #38 convert flow replaced the rest of the #12 pilot ceremony
> (pilot encodes and the watched first replacement). The decision below remains the live setting;
> the one remaining real-clip checkpoint is the #44 acceptance run (delete off, then delete on a
> copy).

## Decision (operator)

| Setting | Value | Notes |
|---|---:|---|
| Daytime video bitrate | **800 kbps** | applies to daytime and motion-heavy footage |
| Evening/night video bitrate | **800 kbps** | applies to evening and dim footage |
| Audio | AAC, 96 kbps | unchanged |
| Output height | 720p | unchanged |
| Evening starts | 17:30 (`Asia/Manila`) | unchanged |
| FFmpeg preset | medium | unchanged |

Rationale: the operator wants a single uniform video bitrate for all footage, and the settings to
stay easily configurable. The motorcycle defaults already ship 800/800, so no configuration change
was required; this record closes checkpoint CP-1 of issue #12 without the candidate comparison.

## Where it is configured

- GUI template editor (`vc gui` → templates) — bitrate fields round-trip through the per-user
  config.
- Per-user config: `~/Library/Application Support/Video Compressor/config.toml`,
  `[templates.motorcycle]` (`video_bitrate_kbps`, `output_height`, ...). Issue #43 collapsed the
  day/evening pair into this single key; a legacy file migrates to the larger of the two values.
- Defaults for new templates: `TemplateSettings` in `src/videocompressor/core/models.py`.

The single video bitrate is one knob; changing it later needs no approval gate.

## Pilot evidence (W2)

The pilot run encodes the selected clips at the single 800 kbps setting (no candidate sweep):

- Outputs under `$PILOT_ROOT/<relative_path>`; source fingerprints unchanged; no `.vc-*`
  artifacts in the archive; pilot jobs settle as `review`.
- Sanitized evidence: `docs/pilot/reports/pilot-run.json` (added by the W2 run).
- Reference frames for a quick operator eyeball: `docs/pilot/review/` (added by the W2 run).

`scripts/bitrate_sweep.py` remains available if a candidate comparison is ever wanted for a
different value.
