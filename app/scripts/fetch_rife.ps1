<#
.SYNOPSIS
  Download and extract rife-ncnn-vulkan + rife-v4.6 weights into
  app/castbooster/{bin,models}/.

.DESCRIPTION
  One-shot setup. Run once per clone. Mirrors fetch_ffmpeg.ps1 conventions:
  sha256 pinning, idempotency, finally-cleanup of temp files.

  Idempotent: if rife-ncnn-vulkan.exe exists AND models/rife-v4.6/ is
  non-empty, the script exits 0 without re-downloading. Use -Force to
  override.

  The upstream Windows zip is ~430MB (it bundles 4 models + macOS/Linux
  artifacts). We extract only the binary and rife-v4.6/ to keep on-disk
  footprint small (~20-30MB after extraction).
#>

[CmdletBinding()]
param(
    [switch]$Force
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$AppRoot    = Split-Path -Parent $ScriptDir
$BinDir     = Join-Path $AppRoot "castbooster\bin"
$ModelsDir  = Join-Path $AppRoot "castbooster\models"
$ModelDir   = Join-Path $ModelsDir "rife-v4.6"
$Archive    = Join-Path $env:TEMP "rife-ncnn-vulkan-20221029-windows.zip"
$ExtractTo  = Join-Path $env:TEMP "rife-ncnn-vulkan-extracted"

# Resolved via `gh api repos/nihui/rife-ncnn-vulkan/releases/tags/20221029`.
$Url = "https://github.com/nihui/rife-ncnn-vulkan/releases/download/20221029/rife-ncnn-vulkan-20221029-windows.zip"

# Pinned sha256 of the upstream Windows zip. Re-fetch verifies this matches.
$ExpectedSha256 = "d8e4d772d26cd8006ef0ad0bc82eb191b53c68677d1ae2f42506d74cbbbea606"

function Test-RifePresent {
    # File-presence check only. Runtime verification of rife (Vulkan ICD load,
    # exit code) is handled by Python's castbooster.license.vulkan_available()
    # — invoking the native exe from PowerShell with stderr capture trips
    # NativeCommandError under $ErrorActionPreference='Stop' (CLAUDE.md hard
    # rule on 2>&1 with native exes). Keep this script narrowly scoped to
    # "are the bits on disk in the right shape".
    $exe = Join-Path $BinDir "rife-ncnn-vulkan.exe"
    if (-not (Test-Path $exe)) { return $false }
    if (-not (Test-Path $ModelDir)) { return $false }
    $files = Get-ChildItem -Path $ModelDir -File -ErrorAction SilentlyContinue
    if ($null -eq $files -or $files.Count -eq 0) { return $false }
    # Binary must be non-empty (extraction completed)
    if ((Get-Item $exe).Length -lt 1000000) { return $false }  # < 1MB = corrupt
    return $true
}

if ((Test-RifePresent) -and -not $Force) {
    Write-Host "rife-ncnn-vulkan + rife-v4.6 already present. Use -Force to re-fetch."
    exit 0
}

New-Item -Path $BinDir    -ItemType Directory -Force | Out-Null
New-Item -Path $ModelsDir -ItemType Directory -Force | Out-Null

try {
    Write-Host "Downloading $Url (~430MB; this takes a few minutes)..."
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
        Write-Host "  This script's `$ExpectedSha256 variable."
        Write-Host "Then re-run with -Force to verify the pin."
    }

    if (Test-Path $ExtractTo) { Remove-Item $ExtractTo -Recurse -Force }
    Expand-Archive -Path $Archive -DestinationPath $ExtractTo -Force

    # Locate rife-ncnn-vulkan.exe inside the extracted tree.
    $SrcExe = Get-ChildItem -Path $ExtractTo -Recurse -Filter "rife-ncnn-vulkan.exe" |
              Select-Object -First 1
    if (-not $SrcExe) { throw "rife-ncnn-vulkan.exe not found inside archive." }
    $SrcRoot = Split-Path -Parent $SrcExe.FullName

    Copy-Item -Path $SrcExe.FullName -Destination $BinDir -Force

    # Copy ONLY rife-v4.6/ (not rife-anime / v2.3 / v4 — we don't ship them).
    # 2026-05-20: rife-anime refused custom -n in the gated test:
    # "only rife-v4 model support custom numframe and timestep". rife-v4.6 is
    # the v4 family's latest and supports arbitrary target_count for 24->60.
    $SrcModel = Join-Path $SrcRoot "rife-v4.6"
    if (-not (Test-Path $SrcModel)) {
        throw "rife-v4.6/ directory not found inside archive at $SrcModel"
    }
    if (Test-Path $ModelDir) { Remove-Item $ModelDir -Recurse -Force }
    Copy-Item -Path $SrcModel -Destination $ModelsDir -Recurse -Force

} finally {
    Remove-Item $Archive   -Force -ErrorAction SilentlyContinue
    Remove-Item $ExtractTo -Recurse -Force -ErrorAction SilentlyContinue
}

# Smoke test
if (-not (Test-RifePresent)) {
    throw "Smoke test failed: rife-ncnn-vulkan.exe was copied but does not run, or rife-v4.6/ is empty."
}
Write-Host "Installed: rife-ncnn-vulkan.exe + rife-v4.6/"
Write-Host "Done."
