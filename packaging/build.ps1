#Requires -Version 5.1
<#
.SYNOPSIS
    Build LanCast for Windows with PyInstaller.

.DESCRIPTION
    Default: a folder build, packaging\dist\LanCast\LanCast.exe. It starts at
    once; distribute the whole LanCast folder (for example as a zip).

    -OneFile: a single packaging\dist\LanCast.exe. Easier to copy around, but
    every start first unpacks about 100 MB into %TEMP%.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File packaging\build.ps1
    powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -OneFile
#>
[CmdletBinding()]
param([switch]$OneFile)

$ErrorActionPreference = 'Stop'

$PackagingDir = $PSScriptRoot
$RepoRoot = Split-Path -Parent $PackagingDir
$Python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
$Spec = Join-Path $PackagingDir 'lancast.spec'

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Python interpreter not found: $Python (create the .venv first, see README)"
}

if ($OneFile) { $env:LANCAST_ONEFILE = '1' } else { Remove-Item Env:LANCAST_ONEFILE -ErrorAction SilentlyContinue }
Push-Location -LiteralPath $RepoRoot
try {
    & $Python -m PyInstaller $Spec --noconfirm `
        --workpath (Join-Path $PackagingDir 'build') `
        --distpath (Join-Path $PackagingDir 'dist')
    $exitCode = $LASTEXITCODE
}
finally {
    Pop-Location
    Remove-Item Env:LANCAST_ONEFILE -ErrorAction SilentlyContinue
}

if ($exitCode -ne 0) {
    throw "PyInstaller failed with exit code $exitCode"
}

if ($OneFile) { $out = Join-Path $PackagingDir 'dist\LanCast.exe' } else { $out = Join-Path $PackagingDir 'dist\LanCast\LanCast.exe' }
Write-Host "Built $out"
