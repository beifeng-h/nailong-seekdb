<#
    Print the telemetry ID of the running SeekDB instance.

    The observer writes it once during bootstrap to
    <base>\run\telemetry.json; the ID is the value of content.id.
#>

param(
    [string]$Runtime = (Join-Path $env:TEMP "seekdb")
)

$ErrorActionPreference = "Stop"

$file = Join-Path $Runtime "base\run\telemetry.json"
if (-not (Test-Path $file)) {
    Write-Error "telemetry.json not found at $file - is seekdb running?"
    exit 1
}

$json = Get-Content $file -Raw | ConvertFrom-Json
Write-Output $json.content.id
