# build_exe.ps1
# Bundles collector.py AND tray.py into two single-file Windows .exes.
# Run from an elevated PowerShell on a build machine (your dev box is fine).
#
# Output:
#   installer\dist\ClaudeUsageCollector.exe  (Scheduled-Task agent)
#   installer\dist\ClaudeUsageTray.exe       (system-tray companion, v1.4+)

$ErrorActionPreference = 'Stop'
$root      = Split-Path -Parent $MyInvocation.MyCommand.Path
$collector = Join-Path $root '..\collector\collector.py' | Resolve-Path
$tray      = Join-Path $root '..\collector\tray.py'      | Resolve-Path
$distDir   = Join-Path $root 'dist'
$buildDir  = Join-Path $root 'build'

Write-Host "Collector source: $collector"
Write-Host "Tray source:      $tray"
Write-Host "Output dist dir:  $distDir"

# 1. Ensure PyInstaller + the tray's runtime deps are available.
$pyiVersion = python -m PyInstaller --version 2>$null
if (-not $pyiVersion) {
    Write-Host "PyInstaller not found - installing..."
    python -m pip install --upgrade pyinstaller
}
# pystray + Pillow are imported by tray.py; pycryptodome is imported by
# desktop_usage.py (the `usage` subcommand). PyInstaller can't bundle
# what isn't installed on the build machine.
Write-Host "Ensuring pystray + Pillow + pycryptodome are installed..."
python -m pip install --quiet --upgrade pystray Pillow pycryptodome

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
$desktopUsage = Join-Path $root '..\collector\desktop_usage.py' | Resolve-Path
# team_activity.py is imported lazily (inside cmd_team_activity), so PyInstaller's
# static analysis won't find it -- bundle it explicitly like desktop_usage.
$teamActivity = Join-Path $root '..\collector\team_activity.py' | Resolve-Path

python -m PyInstaller `
    --onefile `
    --windowed `
    --noconfirm `
    --clean `
    --name 'ClaudeUsageCollector' `
    --distpath $distDir `
    --workpath $buildDir `
    --specpath $buildDir `
    --hidden-import 'desktop_usage' `
    --hidden-import 'team_activity' `
    --hidden-import 'Crypto.Cipher.AES' `
    --hidden-import 'Crypto.Cipher._mode_gcm' `
    --add-data "$($desktopUsage);." `
    --add-data "$($teamActivity);." `
    $collector

if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed for collector (exit $LASTEXITCODE)" }

$exe = Join-Path $distDir 'ClaudeUsageCollector.exe'
if (-not (Test-Path $exe)) { throw "Expected $exe but it was not produced" }

$size = (Get-Item $exe).Length / 1MB
Write-Host ("`nBuilt: {0} ({1:N1} MB)" -f $exe, $size)

# 3. Build the tray app. --windowed because we don't want a console flash
# when launched at login; --onefile for a clean single binary.
Write-Host "`nBuilding tray companion..."
python -m PyInstaller `
    --onefile `
    --windowed `
    --noconfirm `
    --clean `
    --name 'ClaudeUsageTray' `
    --distpath $distDir `
    --workpath $buildDir `
    --specpath $buildDir `
    $tray

if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed for tray (exit $LASTEXITCODE)" }

$trayExe = Join-Path $distDir 'ClaudeUsageTray.exe'
if (-not (Test-Path $trayExe)) { throw "Expected $trayExe but it was not produced" }

$trayMb = (Get-Item $trayExe).Length / 1MB
Write-Host ("`nBuilt: {0} ({1:N1} MB)" -f $trayExe, $trayMb)

Write-Host "`nNext: compile the Inno Setup script (installer\setup.iss) with ISCC."
