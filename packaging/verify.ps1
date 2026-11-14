#Requires -Version 5.1
<#
.SYNOPSIS
    End-to-end verification of packaging\dist\LanCast.exe.

.DESCRIPTION
    * launches the executable with synthetic capture on ports 18743/18742,
      a temporary --data-dir and --no-approval;
    * waits (up to 60 s) for the PIN banner, then runs verify_runtime.py, which
      polls /healthz with urllib, checks /, /static/viewer/app.js and /admin, and
      pairs over WSS with the printed PIN to collect >= 30 H.264 frames;
    * always terminates the LanCast process tree and reports leftover processes.

    Exit code 0 means every check passed.
#>
[CmdletBinding()]
param(
    [int]$AdminPort = 18742,
    [int]$WsPort = 18743,
    [int]$Frames = 30,
    [string]$Python = (Join-Path (Split-Path -Parent $PSScriptRoot) '.venv\Scripts\python.exe'),
    [string]$Exe = (Join-Path $PSScriptRoot 'dist\LanCast\LanCast.exe')
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath $Exe)) {
    throw "Executable not found: $Exe (run build.ps1 first)"
}
if (-not (Test-Path -LiteralPath $Python)) {
    throw "Python interpreter not found: $Python"
}

# Use a temporary data directory, as a real user's first run would.  Fall back
# to the packaging directory if %TEMP% is not writable in this sandbox.
$dataDir = $null
$tempCandidate = Join-Path ([System.IO.Path]::GetTempPath()) ('lancast-verify-' + [guid]::NewGuid().ToString('N'))
try {
    New-Item -ItemType Directory -Path $tempCandidate -Force | Out-Null
    $dataDir = $tempCandidate
}
catch {
    $dataDir = Join-Path $PSScriptRoot ('.verify\run-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
    New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
}

$stdout = Join-Path $dataDir 'server.stdout.log'
$stderr = Join-Path $dataDir 'server.stderr.log'
$process = $null
$exitCode = 1

function Stop-LanCast {
    param($Process)
    if ($null -ne $Process) {
        try {
            & taskkill.exe /PID $Process.Id /T /F 2>$null | Out-Null
        }
        catch { }
        try { $Process.WaitForExit(5000) | Out-Null } catch { }
    }
    Get-Process -Name 'LanCast' -ErrorAction SilentlyContinue | ForEach-Object {
        try { Stop-Process -Id $_.Id -Force -ErrorAction Stop } catch { }
    }
}

try {
    Write-Host "Launching $Exe"
    Write-Host "  data-dir: $dataDir"
    $process = Start-Process -FilePath $Exe -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput $stdout -RedirectStandardError $stderr `
        -ArgumentList @(
            '--capture', 'synthetic',
            '--no-open',
            '--no-approval',
            '--port', "$WsPort",
            '--admin-port', "$AdminPort",
            '--data-dir', $dataDir
        )

    # The banner (with the PIN) is printed only after both listeners are bound.
    $deadline = (Get-Date).AddSeconds(60)
    $pin = $null
    while ((Get-Date) -lt $deadline) {
        if ($process.HasExited) {
            throw "LanCast.exe exited early with code $($process.ExitCode)"
        }
        if (Test-Path -LiteralPath $stdout) {
            $text = Get-Content -LiteralPath $stdout -Raw -Encoding utf8 -ErrorAction SilentlyContinue
            # ASCII-only pattern: the banner uses a full-width colon, which
            # Windows PowerShell 5.1 would mis-read in a UTF-8 script file.
            if ($text -match 'PIN[^\r\n\d]{0,3}(\d{3})[ \t]*(\d{3})') {
                $pin = $Matches[1] + $Matches[2]
                break
            }
        }
        Start-Sleep -Milliseconds 250
    }
    if (-not $pin) {
        throw 'No PIN banner within 60 seconds'
    }
    Write-Host "  PIN: $($pin.Substring(0,3)) $($pin.Substring(3))"

    & $Python (Join-Path $PSScriptRoot 'verify_runtime.py') `
        --admin-port $AdminPort --ws-port $WsPort --data-dir $dataDir --pin $pin --frames $Frames
    $exitCode = $LASTEXITCODE
}
catch {
    Write-Error $_
    $exitCode = 1
}
finally {
    Stop-LanCast -Process $process
    Start-Sleep -Milliseconds 500

    if (Test-Path -LiteralPath $stdout) {
        Write-Host ''
        Write-Host '--- LanCast stdout (tail) ---'
        Get-Content -LiteralPath $stdout -Encoding utf8 -Tail 25
    }
    if (Test-Path -LiteralPath $stderr) {
        $errText = Get-Content -LiteralPath $stderr -Raw -Encoding utf8 -ErrorAction SilentlyContinue
        if ($errText -and $errText.Trim().Length -gt 0) {
            Write-Host ''
            Write-Host '--- LanCast stderr (tail) ---'
            Get-Content -LiteralPath $stderr -Encoding utf8 -Tail 40
        }
    }

    $leftover = @(Get-Process -Name 'LanCast' -ErrorAction SilentlyContinue)
    if ($leftover.Count -gt 0) {
        Write-Error "Leftover LanCast.exe process(es): $($leftover.Id -join ', ')"
        $exitCode = 1
    }
    else {
        Write-Host 'No LanCast.exe processes remain.'
    }
}

exit $exitCode
