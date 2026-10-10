# ffprobe fixtures (issue #2)

Recorded with `ffprobe` 8.1.1 from synthetic clips produced with `ffmpeg` 8.1.1
(HEVC via `libx265` plus AAC, the GoPro container shape). Only the JSON output is
committed; the clips themselves are generated on demand. Recording command:

```sh
ffprobe -v error -print_format json -show_format -show_streams <clip>
```

| Fixture | Shape |
| --- | --- |
| `normal_ztagged.json` | 1 s 320x240 HEVC + mono AAC; the format tag and every stream tag carry `creation_time = 2024-06-14T17:41:00.000000Z` — the GoPro pattern: local wall-clock time with a trailing `Z`. |
| `no_timestamp.json` | `normal_ztagged.json` with every `creation_time` tag removed. |
| `format_stream_conflict.json` | `normal_ztagged.json` with the video stream tag edited to `2024-06-14T09:00:00.000000Z`, disagreeing with the format-level tag. |
| `no_audio.json` | 0.5 s 640x360 HEVC video only, `creation_time` on the format and video stream tags. |
| `tiny_clip.json` | 0.1 s 160x120 HEVC single stream. |

`format.filename` is rewritten to the fixture name; every other field is verbatim
ffprobe output.
