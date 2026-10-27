<#
    Stage the SeekDB Windows build from the bundled MSI.

    The MSI cannot be installed on this machine without administrator
    rights (its service-registration custom action fails), so the payload
    is extracted directly instead: the embedded cabinet is pulled out of
    the MSI's OLE container and expanded, then the MSI's own
    Component/Directory/File tables are used to rebuild the layout.

    Result:
      %TEMP%\seekdb\app\bin     seekdb.exe + runtime libraries
      %TEMP%\seekdb\app\etc     configuration
      %TEMP%\seekdb\app\share   built-in SQL / help / timezone resources

    Requires Python 3.9-3.12 (msilib was removed in 3.13) with the
    bundled olefile under .\tools.
#>

param(
    [string]$Runtime = (Join-Path $env:TEMP "seekdb"),
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"

$ScriptDir = $PSScriptRoot
$Msi = Join-Path $ScriptDir "vendor\seekdb-1.3.0.0-win64.msi"
$Extractor = Join-Path $ScriptDir "tools\msi_extract.py"

if (-not (Test-Path $Msi)) {
    throw "MSI not found: $Msi"
}
if (-not (Test-Path $Extractor)) {
    throw "extractor not found: $Extractor"
}

$AppDir = Join-Path $Runtime "app"
$Stage = Join-Path $Runtime "_stage"
foreach ($d in @($AppDir, $Stage)) {
    if (Test-Path $d) {
        Write-Host "[seekdb] replacing existing $d"
        Remove-Item -LiteralPath $d -Recurse -Force
    }
}
New-Item -ItemType Directory -Force -Path $Stage | Out-Null

Write-Host "[seekdb] extracting $Msi"
$env:PYTHONPATH = Join-Path $ScriptDir "tools"
& $Python $Extractor $Msi $Stage
if ($LASTEXITCODE -ne 0) {
    throw "MSI extraction failed (exit $LASTEXITCODE)"
}

# The MSI lays the payload out under ProgramFiles64Folder\seekdb, so find the
# directory that actually holds bin\seekdb.exe and promote its contents.
$payload = Get-ChildItem $Stage -Recurse -Directory |
    Where-Object { Test-Path (Join-Path $_.FullName "bin\seekdb.exe") } |
    Select-Object -First 1
if (-not $payload) {
    throw "extraction finished but no bin\seekdb.exe found under $Stage"
}

New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
Get-ChildItem $payload.FullName | ForEach-Object {
    Move-Item -LiteralPath $_.FullName -Destination $AppDir -Force
}
Remove-Item -LiteralPath $Stage -Recurse -Force

$exe = Join-Path $AppDir "bin\seekdb.exe"
if (-not (Test-Path $exe)) {
    throw "staging failed, $exe is missing"
}

Write-Host "[seekdb] staged at $AppDir"
Write-Host "[seekdb] next: .\start_seekdb.ps1   (or: python .\launch_detached.py)"