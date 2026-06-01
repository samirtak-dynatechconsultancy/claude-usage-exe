# build_exe.ps1
# Bundles collector.py into a single-file Windows .exe using PyInstaller.
# Run from an elevated PowerShell on a build machine (your dev box is fine).
#
# Output: installer\dist\ClaudeUsageCollector.exe

$ErrorActionPreference = 'Stop'
$root      = Split-Path -Parent $MyInvocation.MyCommand.Path
$collector = Join-Path $root '..\collector\collector.py' | Resolve-Path
$distDir   = Join-Path $root 'dist'
$buildDir  = Join-Path $root 'build'

Write-Host "Collector source: $collector"
Write-Host "Output dist dir:  $distDir"

# 1. Ensure PyInstaller is available.
$pyiVersion = python -m PyInstaller --version 2>$null
if (-not $pyiVersion) {
    Write-Host "PyInstaller not found - installing..."
    python -m pip install --upgrade pyinstaller
}

# 2. Build.
#  --onefile          one self-contained .exe
#  --windowed         GUI subsystem: no console window flash when the
#                     Scheduled Task fires every 15 min. The collector
#                     writes everything to its log file at
#                     %LOCALAPPDATA%\ClaudeUsageCollector\collector.log,
#                     so missing stdout is no real loss. Side effect:
#                     running the exe interactively (e.g. ".\ClaudeUsageCollector.exe
#                     status") prints nothing visibly -- redirect to a
#                     file or read the log to see output.
#  --noconfirm        skip "overwrite?" prompt
#  --clean            wipe PyInstaller cache for repeatable builds
#  --name             output filename (drops the .py)
python -m PyInstaller `
    --onefile `
    --windowed `
    --noconfirm `
    --clean `
    --name 'ClaudeUsageCollector' `
    --distpath $distDir `
    --workpath $buildDir `
    --specpath $buildDir `
    $collector

if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed (exit $LASTEXITCODE)" }

$exe = Join-Path $distDir 'ClaudeUsageCollector.exe'
if (-not (Test-Path $exe)) { throw "Expected $exe but it was not produced" }

$size = (Get-Item $exe).Length / 1MB
Write-Host ("`nBuilt: {0} ({1:N1} MB)" -f $exe, $size)
Write-Host "Next: compile the Inno Setup script (installer\setup.iss) with ISCC."
