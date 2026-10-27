<#
    Start the SeekDB instance.

    Layout:
      %TEMP%\seekdb\app\bin    seekdb.exe + runtime libraries
      %TEMP%\seekdb\app\etc    configuration
      %TEMP%\seekdb\app\share  built-in SQL / help / timezone resources
      %TEMP%\seekdb\base       observer base-dir (store, log, run)

    The runtime lives under %TEMP% because SeekDB hands paths to SQLite as
    UTF-8 while this project's folder name contains non-ASCII characters,
    which SQLite cannot open. Every path the observer touches must therefore
    be plain ASCII.

    The telemetry ID is written by the observer to:
      <base>\run\telemetry.json   ->   content.id
#>

param(
    [int]$Port = 2881,
    [string]$MemoryLimit = "2G",
    [int]$CpuCount = 4,
    [string]$LogLevel = "WARN",
    [switch]$Foreground
)

$ErrorActionPreference = "Stop"

$RuntimeDir = Join-Path $env:TEMP "seekdb"
$AppDir = Join-Path $RuntimeDir "app"
$BaseDir = Join-Path $RuntimeDir "base"
$Exe = Join-Path $AppDir "bin\seekdb.exe"

if (-not (Test-Path $Exe)) {
    throw "seekdb.exe not found at $Exe -- run deploy_seekdb.ps1 first."
}

New-Item -ItemType Directory -Force -Path $BaseDir, (Join-Path $BaseDir "logs") | Out-Null

$rawArgs = @(
    "--base-dir=$BaseDir",
    "--nodaemon",
    "--port=$Port",
    "--parameter", "memory_limit=$MemoryLimit",
    "--parameter", "cpu_count=$CpuCount",
    "--log-level", $LogLevel
)

# Start-Process splits its argument list on whitespace, so quote anything
# that needs it.
$args = $rawArgs | ForEach-Object {
    if ($_ -match '\s') { '"' + $_ + '"' } else { $_ }
}

Write-Host "[seekdb] base-dir : $BaseDir"
Write-Host "[seekdb] command  : $Exe $($args -join ' ')"

if ($Foreground) {
    & $Exe @rawArgs
    exit $LASTEXITCODE
}

$stdout = Join-Path $BaseDir "logs\observer.stdout.log"
$stderr = Join-Path $BaseDir "logs\observer.stderr.log"

$p = Start-Process -FilePath $Exe -ArgumentList $args -NoNewWindow -PassThru `
        -RedirectStandardOutput $stdout -RedirectStandardError $stderr

Set-Content -Path (Join-Path $BaseDir "observer.pid") -Value $p.Id
Write-Host "[seekdb] started, pid=$($p.Id), stdout -> $stdout"
