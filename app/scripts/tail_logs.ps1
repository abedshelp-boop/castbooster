#requires -Version 5.1
<#
.SYNOPSIS
  Tail both Cast Booster log files in one terminal window.

.DESCRIPTION
  Streams %LOCALAPPDATA%\CastBooster\castbooster.log and nm_host.log together,
  prefixing each line with [app] or [nm] so you can tell them apart while
  testing. Ctrl+C to stop.

  The nm_host.log file may not exist until Chrome spawns the native messaging
  bridge for the first time after install; this script tolerates that and
  starts watching once it appears.
#>

$app = Join-Path $env:LOCALAPPDATA 'CastBooster\castbooster.log'
$nm  = Join-Path $env:LOCALAPPDATA 'CastBooster\nm_host.log'

Write-Host ""
Write-Host "Tailing Cast Booster logs (Ctrl+C to stop)" -ForegroundColor Cyan
Write-Host "  [app] $app"
Write-Host "  [nm]  $nm"
Write-Host ""

$jobs = @()

if (Test-Path $app) {
    $jobs += Start-Job -Name 'tail-app' -ScriptBlock {
        param($p)
        Get-Content -Path $p -Wait -Tail 20 | ForEach-Object { "[app] $_" }
    } -ArgumentList $app
} else {
    Write-Host "  (waiting for $app to be created...)" -ForegroundColor Yellow
}

if (Test-Path $nm) {
    $jobs += Start-Job -Name 'tail-nm' -ScriptBlock {
        param($p)
        Get-Content -Path $p -Wait -Tail 20 | ForEach-Object { "[nm]  $_" }
    } -ArgumentList $nm
} else {
    Write-Host "  ($nm not created yet — will be tailed once Chrome spawns the NM host)" -ForegroundColor Yellow
}

try {
    while ($true) {
        $jobs | ForEach-Object { Receive-Job -Job $_ }
        Start-Sleep -Milliseconds 250
    }
}
finally {
    Write-Host ""
    Write-Host "Stopping tail jobs..." -ForegroundColor Cyan
    $jobs | ForEach-Object {
        Stop-Job -Job $_ -ErrorAction SilentlyContinue
        Remove-Job -Job $_ -ErrorAction SilentlyContinue
    }
}
