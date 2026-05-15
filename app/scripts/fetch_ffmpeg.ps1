<#
.SYNOPSIS
  Download and extract BtbN ffmpeg into app/castbooster/bin/.

.DESCRIPTION
  One-shot setup script. Run once per clone (and again when the pinned version
  in app/castbooster/bin/SOURCE.md changes). Verifies sha256 if pinned, prints
  it for first-time pinning otherwise.

  Idempotent: if ffmpeg.exe already exists in bin/, the script exits 0 without
  re-downloading. Use -Force to override.
#>

[CmdletBinding()]
param(
    [switch]$Force
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# Paths relative to this script: ../castbooster/bin/.
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$BinDir    = Join-Path (Split-Path -Parent $ScriptDir) "castbooster\bin"
$Archive   = Join-Path $env:TEMP "ffmpeg-btbn-win64-gpl.zip"
$Url       = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"

# Pin sha256 here once observed (paste from "Observed sha256:" output).
$ExpectedSha256 = "b265df4834c70086373b4515cef9c2407435f80385e776a94a622241ddb3e32b"

function Test-FFmpegPresent {
    $exe = Join-Path $BinDir "ffmpeg.exe"
    if (-not (Test-Path $exe)) { return $false }
    try {
        $line = & $exe -hide_banner -version 2>$null | Select-Object -First 1
        return ($line -match "ffmpeg version")
    } catch {
        return $false
    }
}

if ((Test-FFmpegPresent) -and -not $Force) {
    Write-Host "ffmpeg already present in $BinDir. Use -Force to re-fetch."
    exit 0
}

New-Item -Path $BinDir -ItemType Directory -Force | Out-Null

# $Archive and $ExtractTo are declared before the try so the finally block can
# reference them regardless of where inside the try an exception is thrown.
$ExtractTo = Join-Path $env:TEMP "ffmpeg-btbn-extracted"

try {
    Write-Host "Downloading $Url"
    Invoke-WebRequest -Uri $Url -OutFile $Archive -UseBasicParsing

    $sha = (Get-FileHash -Path $Archive -Algorithm SHA256).Hash.ToLower()
    Write-Host "Observed sha256: $sha"

    if ($null -ne $ExpectedSha256) {
        if ($sha -ne $ExpectedSha256.ToLower()) {
            throw "sha256 mismatch: expected $ExpectedSha256 got $sha."
        }
        Write-Host "sha256 verified."
    } else {
        Write-Host "No pinned sha256 yet. Copy the value above into:"
        Write-Host "  1. app/castbooster/bin/SOURCE.md (replace the TBD line)"
        Write-Host "  2. This script (set `$ExpectedSha256 = '<value>')"
        Write-Host "Then re-run with -Force to verify the pin."
    }

    # Extract with native PowerShell (no 7-Zip needed for .zip).
    if (Test-Path $ExtractTo) { Remove-Item $ExtractTo -Recurse -Force }
    Expand-Archive -Path $Archive -DestinationPath $ExtractTo -Force

    # BtbN archives unpack to ffmpeg-*/bin/{ffmpeg.exe,ffprobe.exe}.
    $SrcBin = Get-ChildItem -Path $ExtractTo -Recurse -Filter "ffmpeg.exe" | Select-Object -First 1
    if (-not $SrcBin) { throw "ffmpeg.exe not found inside archive." }
    $SrcDir  = Split-Path -Parent $SrcBin.FullName
    $Root    = Split-Path -Parent $SrcDir
    $License = Join-Path $Root "LICENSE.txt"

    Copy-Item -Path (Join-Path $SrcDir "ffmpeg.exe")  -Destination $BinDir -Force
    Copy-Item -Path (Join-Path $SrcDir "ffprobe.exe") -Destination $BinDir -Force
    if (Test-Path $License) {
        Copy-Item -Path $License -Destination (Join-Path $BinDir "LICENSE.ffmpeg.txt") -Force
    }
} finally {
    # Always remove temp files even when an exception is thrown above.
    Remove-Item $Archive   -Force -ErrorAction SilentlyContinue
    Remove-Item $ExtractTo -Recurse -Force -ErrorAction SilentlyContinue
}

# Smoke-test: reuse Test-FFmpegPresent so the check is identical to the
# idempotency guard at the top of the script.
if (-not (Test-FFmpegPresent)) {
    throw "Smoke test failed: ffmpeg.exe was copied but does not report a version. Installation may be corrupt or wrong architecture."
}
Write-Host "Installed: $(& (Join-Path $BinDir "ffmpeg.exe") -hide_banner -version 2>$null | Select-Object -First 1)"
Write-Host "Done."
