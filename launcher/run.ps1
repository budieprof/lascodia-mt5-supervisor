# Lascodia supervisor launcher (Windows).  The "Lascodia Supervisor" Scheduled Task (or the HKCU Run
# entry when the task could not be registered) starts this at logon; it keeps the supervisor running:
# start current's Python, wait, start it again 10 s after it exits (a self-update exits on purpose).
#
# Before each start it rolls back a self-update that never came up: if update.pending is past its
# deadline, current.txt goes back to the previous version and the bad one is recorded in update.bad.
# stop.flag (written by the installer while upgrading / uninstalling) ends the loop.
# Installed to <root>\bin\run.ps1 by install.ps1; the copy in each bundle is the source.  ASCII only.
$ErrorActionPreference = 'Continue'
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

$mutex = New-Object System.Threading.Mutex($false, 'Local\LascodiaSupervisorLauncher')
try {
    if (-not $mutex.WaitOne(0)) { exit 0 }   # another launcher already runs for this user
} catch [System.Threading.AbandonedMutexException] { }

function Read-KeyValues([string]$Path) {
    $out = @{}
    if (Test-Path -LiteralPath $Path) {
        foreach ($line in Get-Content -LiteralPath $Path) {
            $i = $line.IndexOf('=')
            if ($i -gt 0) { $out[$line.Substring(0, $i).Trim()] = $line.Substring($i + 1).Trim() }
        }
    }
    return $out
}

function Write-LauncherLog([string]$Text) {
    $logs = Join-Path $Root 'logs'
    New-Item -ItemType Directory -Force -Path $logs | Out-Null
    Add-Content -LiteralPath (Join-Path $logs 'launcher.log') -Value ((Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + ' ' + $Text)
}

function Invoke-RollbackCheck {
    $pending = Join-Path $Root 'update.pending'
    $kv = Read-KeyValues $pending
    if ($kv.Count -eq 0) { return }
    $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $deadline = 0
    [void][long]::TryParse([string]$kv['deadline'], [ref]$deadline)
    $from = [string]$kv['from']
    if ($deadline -gt 0 -and $now -ge $deadline -and $from -and (Test-Path -LiteralPath (Join-Path $Root "versions\$from"))) {
        Set-Content -LiteralPath (Join-Path $Root 'current.txt') -Value $from -Encoding ASCII
        Add-Content -LiteralPath (Join-Path $Root 'update.bad') -Value ([string]$kv['to']) -Encoding ASCII
        Move-Item -LiteralPath $pending -Destination (Join-Path $Root 'update.rolledback') -Force
        Write-LauncherLog ("version " + $kv['to'] + " did not come up; rolled back to " + $from)
    }
}

$env:LASCODIA_SUPERVISOR_HOME = $Root
$env:LASCODIA_SUPERVISOR_CONFIG = Join-Path $Root 'config\supervisor.yaml'
$stopFlag = Join-Path $Root 'stop.flag'

while ($true) {
    if (Test-Path -LiteralPath $stopFlag) { break }
    Invoke-RollbackCheck
    $version = ''
    $ptr = Join-Path $Root 'current.txt'
    if (Test-Path -LiteralPath $ptr) { $version = ((Get-Content -LiteralPath $ptr -TotalCount 1) + '').Trim() }
    $py = Join-Path $Root "versions\$version\python\python.exe"
    if (-not $version -or -not (Test-Path -LiteralPath $py)) {
        Write-LauncherLog "no runnable version (current.txt says '$version'); retrying in 60 s"
        Start-Sleep -Seconds 60
        continue
    }
    $logs = Join-Path $Root 'logs'
    New-Item -ItemType Directory -Force -Path $logs | Out-Null
    try {
        $p = Start-Process -FilePath $py -ArgumentList @('-I', '-m', 'lascodia_supervisor', 'run') `
            -WorkingDirectory $Root -NoNewWindow -Wait -PassThru `
            -RedirectStandardOutput (Join-Path $logs 'stdout.log') -RedirectStandardError (Join-Path $logs 'stderr.log')
        Write-LauncherLog ("supervisor $version exited with code " + $p.ExitCode)
    } catch {
        Write-LauncherLog ("could not start supervisor ${version}: " + $_.Exception.Message)
    }
    if (Test-Path -LiteralPath $stopFlag) { break }
    Start-Sleep -Seconds 10
}
