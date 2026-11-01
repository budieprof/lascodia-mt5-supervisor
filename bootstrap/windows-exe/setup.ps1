# Lascodia supervisor - the double-click Windows installer's worker (runs inside "Lascodia Setup <code>.exe").
#
# The .exe (bootstrap/windows-exe/installer.nsi) unpacks this file and the engine's own one-line
# installer (install.ps1, with the engine address already filled in) into a private temp folder
# and runs:  powershell -File setup.ps1 -ExeName "<the .exe's file name>" [-Silent]
#
# It finds the install code in the .exe's own file name - the console saves the download as
# "Lascodia Setup <code>.exe" - asks for it when the file was renamed, runs install.ps1 for the
# person signed in to Windows (no administrator rights needed), keeps a copy of everything it
# printed in %LOCALAPPDATA%\Lascodia\Supervisor Install.log and exits 0 only when the supervisor
# connected.  On failure it shows the reason in a message box (not with -Silent) and exits 1.
#
# Test hooks (environment): LASCODIA_EXE_CODE (the code, skips the file name), LASCODIA_EXE_NO_DIALOG=1
# (never show a dialog), LASCODIA_EXE_LOG (log path), plus every hook install.ps1 reads.
# ASCII only.
param(
    [string]$ExeName = '',
    [switch]$Silent
)

$ErrorActionPreference = 'Stop'
$CodeRe = 'lsc_enr_[a-z0-9]{8}_[A-Za-z0-9]{40}'
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$NoDialog = $Silent -or ($env:LASCODIA_EXE_NO_DIALOG -eq '1')

function Say([string]$Text) { [Console]::Out.WriteLine($Text) }

function Show-Box([string]$Text, [string]$Title, [string]$Icon) {
    if ($NoDialog) { return }
    try {
        Add-Type -AssemblyName System.Windows.Forms
        [void][System.Windows.Forms.MessageBox]::Show($Text, $Title, 'OK', $Icon)
    } catch { }
}

$Log = $env:LASCODIA_EXE_LOG
if (-not $Log) {
    $base = $env:LOCALAPPDATA
    if (-not $base) { $base = Join-Path $env:USERPROFILE 'AppData\Local' }
    $Log = Join-Path $base 'Lascodia\Supervisor Install.log'
}
try {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Log) | Out-Null
    Set-Content -LiteralPath $Log -Value ("Lascodia supervisor install " + (Get-Date).ToString('u')) -Encoding UTF8
} catch { $Log = '' }
function Keep([string]$Text) {
    Say $Text
    if ($Log) { try { Add-Content -LiteralPath $Log -Value $Text -Encoding UTF8 } catch { } }
}

# ---- the install code -----------------------------------------------------------------------
$Code = ''
if ($env:LASCODIA_EXE_CODE) { $Code = $env:LASCODIA_EXE_CODE }
elseif ($ExeName -cmatch $CodeRe) { $Code = $Matches[0] }
if (-not $Code -and -not $NoDialog) {
    # The file was renamed (or saved without the console): the person pastes the code - or the
    # whole install line, the code is picked out of it.
    try {
        Add-Type -AssemblyName Microsoft.VisualBasic
        $typed = [Microsoft.VisualBasic.Interaction]::InputBox(
            "This installer's file name does not carry its install code (it starts with lsc_enr_).`r`n`r`n" +
            "In the console open Terminals, click Add a computer, copy the install line and paste it here.",
            'Lascodia Supervisor', '')
        if ($typed -cmatch $CodeRe) { $Code = $Matches[0] }
    } catch { }
}
if (-not $Code) {
    Keep 'Install failed: no install code.'
    Keep 'Download the installer again from Add a computer in the console and do not rename the file.'
    Show-Box "No install code, so nothing was installed.`r`n`r`nDownload the installer again from Add a computer in the console and do not rename the file." 'The supervisor was not installed' 'Error'
    exit 1
}

# ---- run the engine's one-line installer ----------------------------------------------------
$Script = Join-Path $Here 'install.ps1'
if (-not (Test-Path -LiteralPath $Script)) {
    Keep 'Install failed: this installer is incomplete (install.ps1 is missing).'
    Show-Box 'This installer is damaged. Download it again from the console.' 'The supervisor was not installed' 'Error'
    exit 1
}
Keep "Installing the Lascodia supervisor for $env:USERNAME..."
$lines = New-Object System.Collections.Generic.List[string]
$ErrorActionPreference = 'Continue'
try {
    # *>&1: install.ps1 talks through Write-Host (the information stream); fold every stream into
    # the pipeline so each line reaches the installer window as it happens.
    & $Script $Code *>&1 | ForEach-Object {
        $s = [string]$_
        $lines.Add($s)
        Keep $s
    }
} catch {
    $s = 'Install failed: ' + $_.Exception.Message
    $lines.Add($s)
    Keep $s
}
$ErrorActionPreference = 'Stop'

# install.ps1 never exits (it is written for a window the person keeps): judge it by what it said.
$failedAt = -1
for ($i = 0; $i -lt $lines.Count; $i++) { if ($lines[$i].TrimStart().StartsWith('Install failed:')) { $failedAt = $i; break } }
$connected = @($lines | Where-Object { $_.TrimStart().StartsWith('Connected:') -or $_.TrimStart().StartsWith('Installed supervisor') }).Count -gt 0
$notYet = @($lines | Where-Object { $_.TrimStart().StartsWith('Not connected yet:') -or $_.TrimStart().StartsWith('The supervisor did not start') })

if ($failedAt -lt 0 -and $connected) {
    Keep 'Done.'
    exit 0
}
if ($failedAt -ge 0) {
    $why = $lines[$failedAt].TrimStart().Substring('Install failed:'.Length).Trim()
    if ($failedAt + 1 -lt $lines.Count) { $why = $why + "`r`n" + $lines[$failedAt + 1].Trim() }
} elseif ($notYet.Count -gt 0) {
    $why = $notYet[0].Trim()
} else {
    $why = (($lines | Select-Object -Last 3) -join "`r`n").Trim()
    if (-not $why) { $why = 'The installer stopped without saying why.' }
}
$where = 'The full output is in %LOCALAPPDATA%\Lascodia\Supervisor Install.log.'
Show-Box ($why + "`r`n`r`n" + $where) 'The supervisor was not installed' 'Error'
exit 1
