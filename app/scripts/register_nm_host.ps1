#Requires -Version 5.1
<#
.SYNOPSIS
  Register the Cast Booster native messaging host with Chrome.

.DESCRIPTION
  Writes a manifest JSON that points Chrome at nm_host.bat, and adds an HKCU
  registry entry so Chrome discovers it. No admin required — everything runs
  under the current user.

  You must pass the unpacked extension ID. To get it:
    1. Open chrome://extensions
    2. Toggle "Developer mode" on
    3. Click "Load unpacked" and pick <repo>/extension
    4. Copy the ID shown under Cast Booster

.PARAMETER ExtensionId
  The Chrome extension ID (32-char lowercase). Required.

.EXAMPLE
  pwsh -ExecutionPolicy Bypass -File .\register_nm_host.ps1 -ExtensionId abcdefghijklmnopabcdefghijklmnop
#>
param(
  [Parameter(Mandatory = $true)]
  [ValidatePattern('^[a-p]{32}$')]
  [string]$ExtensionId
)

$ErrorActionPreference = 'Stop'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$batPath = Join-Path $scriptDir 'nm_host.bat'
if (-not (Test-Path $batPath)) {
  throw "nm_host.bat not found at $batPath"
}

$manifestPath = Join-Path $scriptDir 'com.castbooster.host.generated.json'

$manifest = [ordered]@{
  name = 'com.castbooster.host'
  description = 'Cast Booster native messaging host'
  path = $batPath
  type = 'stdio'
  allowed_origins = @("chrome-extension://$ExtensionId/")
}

$json = $manifest | ConvertTo-Json -Depth 4
# Native messaging manifests must be plain UTF-8 (no BOM).
[System.IO.File]::WriteAllText($manifestPath, $json, (New-Object System.Text.UTF8Encoding($false)))

$regPath = 'HKCU:\Software\Google\Chrome\NativeMessagingHosts\com.castbooster.host'
if (-not (Test-Path $regPath)) {
  New-Item -Path $regPath -Force | Out-Null
}
Set-ItemProperty -Path $regPath -Name '(default)' -Value $manifestPath

Write-Host ''
Write-Host 'Cast Booster native messaging host registered.' -ForegroundColor Green
Write-Host "  Manifest: $manifestPath"
Write-Host "  Registry: $regPath"
Write-Host "  Extension: chrome-extension://$ExtensionId/"
Write-Host ''
Write-Host 'Restart Chrome to pick up the registration.'
