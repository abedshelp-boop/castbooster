#Requires -Version 5.1
<#
.SYNOPSIS
  Remove the Cast Booster native messaging host registration.
#>
$ErrorActionPreference = 'Stop'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$manifestPath = Join-Path $scriptDir 'com.castbooster.host.generated.json'
$regPath = 'HKCU:\Software\Google\Chrome\NativeMessagingHosts\com.castbooster.host'

if (Test-Path $regPath) {
  Remove-Item -Path $regPath -Recurse -Force
  Write-Host "Removed registry key $regPath" -ForegroundColor Green
} else {
  Write-Host "Registry key not present at $regPath"
}

if (Test-Path $manifestPath) {
  Remove-Item -Path $manifestPath -Force
  Write-Host "Removed manifest $manifestPath" -ForegroundColor Green
}
