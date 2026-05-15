# Vendored ffmpeg Source

These binaries are NOT committed to git. Run `app\scripts\fetch_ffmpeg.ps1`
from PowerShell after cloning to populate this directory.

## Pinned source

- **Source**: BtbN FFmpeg-Builds — https://github.com/BtbN/FFmpeg-Builds/releases
- **Variant**: `ffmpeg-master-latest-win64-gpl.zip` (rolling latest master, GPL build)
- **Archive URL**: https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip
- **Archive sha256**: `b265df4834c70086373b4515cef9c2407435f80385e776a94a622241ddb3e32b`

## Contents (after running fetch_ffmpeg.ps1)

- `ffmpeg.exe` — encoder/decoder (~120 MB)
- `ffprobe.exe` — stream introspection (~120 MB)
- `LICENSE.ffmpeg.txt` — license notices from the upstream build

## Why BtbN

BtbN ships `.zip` archives that PowerShell extracts natively via `Expand-Archive`
(no 7-Zip system dependency). Auto-built from FFmpeg master with the GPL build
flags, so includes libx264, libx265, libass (subtitle burn-in for P2.3), libplacebo
(potentially useful in P3/P4), and the relevant HW encoders (nvenc, qsv, amf).

Functionally identical to Gyan 8.1 (which we initially specced) for our purposes.
