# Lascodia supervisor - one-line installer for Windows (Windows PowerShell 5.1 or PowerShell 7).
#
#   & ([scriptblock]::Create((irm <api>/onboarding/supervisor/install.ps1))) <INSTALL-CODE>
#   & ([scriptblock]::Create((irm <api>/onboarding/supervisor/install.ps1))) --uninstall
#
# Downloads the self-contained supervisor (its own Python; nothing else is installed), checks its
# sha256, installs it in %ProgramData%\Lascodia\Supervisor, finds the MetaTrader 5 terminals on this
# computer, registers the "Lascodia Supervisor" Scheduled Task (at logon of this user: MetaTrader is a
# desktop app and must run in this user's session), starts it and waits until it has connected to
# the engine.  Running it again upgrades in place and keeps this computer's identity.  It never
# starts, stops or changes a MetaTrader terminal itself.
#
# The engine fills in __LASCODIA_API_BASE__ when it serves this file.  No secrets live here: the
# install code is the only credential and it arrives as the argument.  This script runs inside the
# user's own PowerShell window, so it never calls exit (that would close the window): failures
# print what to do and return.  ASCII only (irm may decode the script as Latin-1).
#
# Test hooks (environment): LASCODIA_API_BASE, LASCODIA_BUNDLE_FILE (+ LASCODIA_BUNDLE_SHA256),
# LASCODIA_SKIP_SERVICE=1, LASCODIA_INSTALL_ROOT, LASCODIA_WAIT_SEC (default 60).
param(
    [Parameter(Position = 0)][string]$Code = '',
    [switch]$Uninstall
)

function Invoke-LascodiaInstall {
    param([string]$Code, [bool]$Uninstall)

    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'   # the progress bar makes Invoke-WebRequest 10x slower on 5.1

    $ApiBase = '__LASCODIA_API_BASE__'
    if ($ApiBase.StartsWith('__LASCODIA')) { $ApiBase = '' }
    if ($env:LASCODIA_API_BASE) { $ApiBase = $env:LASCODIA_API_BASE }
    $ApiBase = $ApiBase.TrimEnd('/')
    $programData = $env:ProgramData
    if (-not $programData) { $programData = 'C:\ProgramData' }
    $Root = $programData.TrimEnd('\') + '\Lascodia\Supervisor'
    if ($env:LASCODIA_INSTALL_ROOT) { $Root = $env:LASCODIA_INSTALL_ROOT }
    $TaskName = 'Lascodia Supervisor'
    $RunKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
    $RunValue = 'LascodiaSupervisor'
    $WaitSec = 60
    if ($env:LASCODIA_WAIT_SEC) { $WaitSec = [int]$env:LASCODIA_WAIT_SEC }
    $SkipService = ($env:LASCODIA_SKIP_SERVICE -eq '1')
    $User = "$env:USERDOMAIN\$env:USERNAME"

    function Say([string]$Text) { Write-Host $Text }
    function Fail([string]$What, [string]$Todo = '') {
        Write-Host ''
        Write-Host "Install failed: $What" -ForegroundColor Red
        if ($Todo) { Write-Host $Todo }
    }

    if ($Code -in @('--uninstall', '-uninstall', 'uninstall')) { $Uninstall = $true; $Code = '' }

    function Stop-Supervisor {
        # Ends the launcher loop and the supervisor.  MetaTrader terminals are separate processes
        # outside $Root and are never touched.
        New-Item -ItemType Directory -Force -Path $Root | Out-Null
        Set-Content -LiteralPath (Join-Path $Root 'stop.flag') -Value 'stop' -Encoding ASCII
        if ($SkipService) { return }
        try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } catch { }
        $rootLike = $Root.TrimEnd('\') + '\*'
        $procs = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
            ($_.ExecutablePath -and $_.ExecutablePath -like $rootLike) -or
            ($_.CommandLine -and $_.CommandLine -like ('*' + $Root + '\bin\run.ps1*'))
        })
        foreach ($p in $procs) { try { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue } catch { } }
        Start-Sleep -Seconds 2
    }

    # ---- uninstall ---------------------------------------------------------------------------
    if ($Uninstall) {
        Say 'Removing the Lascodia supervisor from this computer...'
        Stop-Supervisor
        if (-not $SkipService) {
            try { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue } catch { }
            try { Remove-ItemProperty -Path $RunKey -Name $RunValue -ErrorAction SilentlyContinue } catch { }
        }
        try { Remove-Item -LiteralPath $Root -Recurse -Force -ErrorAction Stop } catch {
            Fail "could not delete $Root ($($_.Exception.Message))." 'Close any window showing that folder and run the line again.'
            return
        }
        Say 'Removed. Your MetaTrader terminals were not touched.'
        Say 'To connect this computer again later, use Add a computer in the console (a new line is needed).'
        return
    }

    # ---- checks ------------------------------------------------------------------------------
    if (-not $Code) {
        Fail 'this line is missing its install code.' 'In the console open Terminals, click Add a computer and copy the whole line again.'
        return
    }
    if (-not $ApiBase) {
        Fail 'this copy of the installer does not know where the engine is.' 'Copy the line from the console again.'
        return
    }
    if (-not [Environment]::Is64BitOperatingSystem) {
        Fail 'this computer runs 32-bit Windows.' 'The supervisor needs 64-bit Windows 10 or later.'
        return
    }
    $Platform = 'windows-x64'
    $Start = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    } catch { }
    $Tmp = Join-Path ([IO.Path]::GetTempPath()) ('lascodia-install-' + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Force -Path $Tmp | Out-Null

    try {
        Say "Installing the Lascodia supervisor on this computer ($Platform)..."

        # ---- download + verify ---------------------------------------------------------------
        $Zip = Join-Path $Tmp 'bundle.zip'
        $Sha = ''
        if ($env:LASCODIA_BUNDLE_FILE) {
            Copy-Item -LiteralPath $env:LASCODIA_BUNDLE_FILE -Destination $Zip
            $Sha = [string]$env:LASCODIA_BUNDLE_SHA256
        } else {
            Say '  Downloading...'
            $url = "$ApiBase/onboarding/supervisor/bundle?platform=$Platform"
            try {
                $resp = Invoke-WebRequest -Uri $url -Headers @{ 'X-Enrollment-Token' = $Code } -OutFile $Zip -PassThru -UseBasicParsing -TimeoutSec 600
            } catch {
                $status = 0
                try { $status = [int]$_.Exception.Response.StatusCode } catch { }
                $hostPart = ($ApiBase -split '/api/')[0]
                if ($status -eq 0) { Fail "could not reach $hostPart." "Check this computer's internet connection, then run the line again. ($($_.Exception.Message))" }
                elseif ($status -in @(400, 401, 403, 410)) { Fail 'the install code in this line was not accepted (it may have expired or already been used).' 'In the console, click Add a computer again to get a fresh line.' }
                elseif ($status -eq 404) { Fail "no supervisor download for $Platform is published yet." 'Ask your administrator to publish one, then run the line again.' }
                else { Fail "the engine answered HTTP $status to the download." 'Wait a minute and run the line again.' }
                return
            }
            $h = $resp.Headers['X-Bundle-Sha256']
            if ($h -is [array]) { $h = $h[0] }
            $Sha = [string]$h
            if (-not $Sha) { Fail 'the engine did not say which download to expect (no checksum).' 'Ask your administrator to update the engine.'; return }
        }
        $got = (Get-FileHash -LiteralPath $Zip -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($Sha -and ($Sha.Trim().ToLowerInvariant() -ne $got)) {
            Fail 'the download is damaged (checksum mismatch).' 'Run the line again; if it keeps failing, tell your administrator.'
            return
        }

        # ---- unpack + check ------------------------------------------------------------------
        foreach ($d in @('versions', 'bin', 'logs', 'config')) { New-Item -ItemType Directory -Force -Path (Join-Path $Root $d) | Out-Null }
        # The config folder holds this computer's credential: only this user, SYSTEM and Administrators.
        if (-not (Get-Command icacls -ErrorAction SilentlyContinue)) {
            Fail 'this computer has no icacls, so the settings folder cannot be protected.' 'Tell your administrator.'
            return
        }
        & icacls (Join-Path $Root 'config') /inheritance:r /grant:r "${User}:(OI)(CI)F" '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Fail 'could not protect the settings folder (icacls failed).' "Check that you can change permissions on $Root, then run the line again."
            return
        }
        $Stage = Join-Path $Root ('versions\.staging-' + $PID)
        if (Test-Path -LiteralPath $Stage) { Remove-Item -LiteralPath $Stage -Recurse -Force }
        try { Expand-Archive -LiteralPath $Zip -DestinationPath $Stage -Force } catch {
            Fail 'could not unpack the download.' 'Free some disk space and run the line again.'
            return
        }
        $Version = ''
        if (Test-Path -LiteralPath (Join-Path $Stage 'VERSION')) { $Version = (Get-Content -LiteralPath (Join-Path $Stage 'VERSION') -TotalCount 1).Trim() }
        $py = Join-Path $Stage 'python\python.exe'
        $said = ''
        if ($Version -and (Test-Path -LiteralPath $py)) {
            try {
                & $py -I -m lascodia_supervisor verify-bundle $Stage | Out-Null
                if ($LASTEXITCODE -eq 0) { $said = ((& $py -I -m lascodia_supervisor version) | Out-String).Trim() }
            } catch { $said = '' }
        }
        if (-not $Version -or $said -ne $Version) {
            Remove-Item -LiteralPath $Stage -Recurse -Force -ErrorAction SilentlyContinue
            Fail 'the downloaded supervisor does not run on this computer.' "Tell your administrator (platform $Platform)."
            return
        }

        # ---- swap in -------------------------------------------------------------------------
        if (Test-Path -LiteralPath (Join-Path $Root 'config\supervisor.yaml')) {
            Say '  Upgrading the supervisor already on this computer (its settings are kept)...'
        }
        Stop-Supervisor
        $Final = Join-Path $Root "versions\$Version"
        if (Test-Path -LiteralPath $Final) { Remove-Item -LiteralPath $Final -Recurse -Force }
        Move-Item -LiteralPath $Stage -Destination $Final
        Set-Content -LiteralPath (Join-Path $Root 'current.txt') -Value $Version -Encoding ASCII
        Remove-Item -LiteralPath (Join-Path $Root 'update.pending') -Force -ErrorAction SilentlyContinue
        Copy-Item -LiteralPath (Join-Path $Final 'launcher\run.ps1') -Destination (Join-Path $Root 'bin\run.ps1') -Force
        # Keep the three newest versions (self-update rolls back to the previous one).
        Get-ChildItem -LiteralPath (Join-Path $Root 'versions') -Directory | Where-Object { -not $_.Name.StartsWith('.') } |
            Sort-Object LastWriteTime -Descending | Select-Object -Skip 3 | Where-Object { $_.Name -ne $Version } |
            ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }
        $py = Join-Path $Final 'python\python.exe'

        $env:LASCODIA_ENROLLMENT_TOKEN = $Code
        # 5.1 turns a native command's stderr into terminating errors under 'Stop': relax it here.
        $ErrorActionPreference = 'Continue'
        try {
            $out = & $py -I -m lascodia_supervisor setup --root $Root --engine-url $ApiBase 2>&1
            $rc = $LASTEXITCODE
        } finally {
            Remove-Item Env:\LASCODIA_ENROLLMENT_TOKEN -ErrorAction SilentlyContinue
            $ErrorActionPreference = 'Stop'
        }
        foreach ($line in @($out)) { Say ('  ' + $line) }
        if ($rc -ne 0) { Fail 'could not write the supervisor settings.' 'Run the line again; if it keeps failing, tell your administrator.'; return }
        Remove-Item -LiteralPath (Join-Path $Root 'stop.flag') -Force -ErrorAction SilentlyContinue

        if ($SkipService) {
            Say "Installed supervisor $Version (test mode: the background service was not started)."
            return
        }

        # ---- start at logon + now ------------------------------------------------------------
        Say '  Starting...'
        $ps = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
        $arg = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + (Join-Path $Root 'bin\run.ps1') + '"'
        $how = 'task'
        try {
            $action = New-ScheduledTaskAction -Execute $ps -Argument $arg -WorkingDirectory $Root
            $trigger = New-ScheduledTaskTrigger -AtLogOn -User $User
            $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
                -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
            $principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
            Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
            try { Remove-ItemProperty -Path $RunKey -Name $RunValue -ErrorAction SilentlyContinue } catch { }
            Start-ScheduledTask -TaskName $TaskName
        } catch {
            # Some locked-down computers do not let a normal user add a logon task: start from the
            # user's Run key instead (the launcher itself restarts the supervisor if it stops).
            $how = 'run-key'
            New-ItemProperty -Path $RunKey -Name $RunValue -Value ('"' + $ps + '" ' + $arg) -PropertyType String -Force | Out-Null
            Start-Process -FilePath $ps -ArgumentList $arg -WindowStyle Hidden
        }
        if ($how -eq 'run-key') { Say '  (Started from your sign-in items; a scheduled task was not allowed on this computer.)' }

        Say "  Waiting for it to connect to the engine (up to $WaitSec s)..."
        & $py -I -m lascodia_supervisor wait-registered --root $Root --since $Start --timeout $WaitSec
    } catch {
        Fail ($_.Exception.Message) 'Run the line again; if it keeps failing, send this message to your administrator.'
    } finally {
        Remove-Item -LiteralPath $Tmp -Recurse -Force -ErrorAction SilentlyContinue
    }
}

Invoke-LascodiaInstall -Code $Code -Uninstall ([bool]$Uninstall)
