# Vendored ffmpeg Source

These binaries are NOT committed to git. Run `app\scripts\fetch_ffmpeg.ps1`
from PowerShell after cloning to populate this directory.

## Pinned source

- **Source**: Gyan FFmpeg builds — https://www.gyan.dev/ffmpeg/builds/
- **Version**: 8.1 full_build
- **Archive**: `ffmpeg-8.1-full_build.7z`
- **Archive URL**: https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-8.1-full_build.7z
- **Archive sha256**: TBD — captured by fetch_ffmpeg.ps1 on first successful run and pinned here.

## Contents (after running fetch_ffmpeg.ps1)

- `ffmpeg.exe` — encoder/decoder (~120 MB)
- `ffprobe.exe` — stream introspection (~120 MB)
- `LICENSE.ffmpeg.txt` — license notices from the upstream build

## Why Gyan

Single trusted maintainer with versioned URLs. Matches the build Abed runs
locally on PATH, so dev/prod behavior is identical. Full build includes
libass (subtitle burn-in for P2.3) and libplacebo (potentially useful in P3/P4).
