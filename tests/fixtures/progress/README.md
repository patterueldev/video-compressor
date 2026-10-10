# Progress fixtures

Real `ffmpeg -progress pipe:1` output captured while exercising the production
two-pass recipe on a 6 s 1080p `testsrc2` + `sine` clip (ffmpeg 8.1.1, macOS),
scaled to 720p, 800 kbps video, AAC 96 kbps. `pass1.txt` uses the null muxer
(`total_size=N/A`), `pass2.txt` writes the MP4 output.

The parser tests replay these files and assert structure (monotonic `out_time`,
percent/ETA present, final block reaching the end); they contain nothing specific
to the machine that generated them.
