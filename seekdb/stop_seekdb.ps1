<#
    Stop the running SeekDB observer.

    There is no graceful SQL shutdown in SeekDB, so the process is
    terminated. The observer is crash-safe: it replays its log on the next
    start.
#>

param(
    [string]$Runtime = (Join-Path $env:TEMP "seekdb")
)

$ErrorActionPreference = "Stop"

$pidFile = Join-Path $Runtime "base\observer.pid"

# Only touch an observer that belongs to this runtime, never an unrelated
# seekdb.exe that happens to be running on the machine.
$exePrefix = Join-Path $Runtime "app\bin\seekdb.exe"
$procs = Get-Process seekdb -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -and $_.Path.Equals($exePrefix, [StringComparison]::OrdinalIgnoreCase) }

if (-not $procs) {
    Write-Host "[seekdb] no observer running for $Runtime"
    exit 0
}

foreach ($p in $procs) {
    Write-Host "[seekdb] stopping pid=$($p.Id)"
    Stop-Process -Id $p.Id -Force
}

Start-Sleep -Seconds 3
if (Get-Process seekdb -ErrorAction SilentlyContinue) {
    Write-Host "[seekdb] warning: process still present"
} else {
    Write-Host "[seekdb] stopped"
}
