; Lascodia supervisor - double-click Windows installer ("Lascodia Setup <code>.exe").
;
; Built on any machine with NSIS (makensis) by tools/build_exe.sh, which passes:
;   /DVERSION=1.0.0  /DOUTFILE=...exe  /DPAYLOAD=<folder with setup.ps1 + the engine's install.ps1>
;
; It installs nothing by itself: it unpacks setup.ps1 and install.ps1 into a private temp folder and
; runs setup.ps1 with the .exe's own file name (the install code is in it), for the person signed
; in to Windows - no administrator rights needed (the supervisor runs in that person's session, as
; MetaTrader does).  Unsigned on purpose (no code-signing certificate): Windows SmartScreen asks the
; person to confirm it once (More info -> Run anyway).
;
; Silent:  "Lascodia Setup <code>.exe" /S   -> exit code 0 = connected, 1 = not installed (log in
; %LOCALAPPDATA%\Lascodia\Supervisor Install.log), for IT departments that deploy it remotely.

Unicode true
SetCompressor /SOLID lzma
SetDateSave off   ; same inputs -> same bytes (the engine refuses other bytes under one version)
!include "MUI2.nsh"
!include "LogicLib.nsh"

!ifndef VERSION
  !error "pass /DVERSION=x.y.z"
!endif
!ifndef OUTFILE
  !error "pass /DOUTFILE=path.exe"
!endif
!ifndef PAYLOAD
  !error "pass /DPAYLOAD=folder"
!endif

Name "Lascodia Supervisor"
Caption "Lascodia Supervisor setup"
OutFile "${OUTFILE}"
RequestExecutionLevel user
ShowInstDetails show
BrandingText "Lascodia"
InstallDir "$TEMP"

VIProductVersion "${VERSION}.0"
VIAddVersionKey "ProductName" "Lascodia Supervisor setup"
VIAddVersionKey "FileDescription" "Connects this computer's MetaTrader 5 terminals to Lascodia"
VIAddVersionKey "CompanyName" "Lascodia"
VIAddVersionKey "LegalCopyright" "Lascodia"
VIAddVersionKey "FileVersion" "${VERSION}"
VIAddVersionKey "ProductVersion" "${VERSION}"

!define MUI_ABORTWARNING
!define MUI_WELCOMEPAGE_TITLE "Connect this computer to Lascodia"
!define MUI_WELCOMEPAGE_TEXT "This installs the Lascodia supervisor, a small helper that runs in the background and looks after the MetaTrader 5 terminals on this computer.$\r$\n$\r$\nIt is installed for you (the person signed in to Windows) and starts every time you sign in. It finds your MetaTrader 5 terminals and connects them to your organisation. It does not change your terminals or anything else on this computer.$\r$\n$\r$\nIt takes about a minute. Keep the console page open: it shows this computer connecting.$\r$\n$\r$\nClick Install to continue."
!define MUI_PAGE_CUSTOMFUNCTION_SHOW WelcomeShow
!insertmacro MUI_PAGE_WELCOME
!define MUI_PAGE_HEADER_TEXT "Connecting this computer"
!define MUI_PAGE_HEADER_SUBTEXT "Installing the Lascodia supervisor and connecting it to your organisation..."
!define MUI_INSTFILESPAGE_FINISHHEADER_TEXT "Connected"
!define MUI_INSTFILESPAGE_FINISHHEADER_SUBTEXT "The supervisor is installed and connected."
!define MUI_INSTFILESPAGE_ABORTHEADER_TEXT "Not installed"
!define MUI_INSTFILESPAGE_ABORTHEADER_SUBTEXT "The supervisor was not installed. The reason is in the list above."
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_TITLE "This computer is connected"
!define MUI_FINISHPAGE_TEXT "The Lascodia supervisor is running and connected to your organisation.$\r$\n$\r$\nGo back to the console to finish: it lists the MetaTrader 5 terminals it found and anything still to do.$\r$\n$\r$\nYou can delete this installer now."
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_LANGUAGE "English"

; The welcome page's button says Install: there is nothing to choose, the next page installs.
Function WelcomeShow
  GetDlgItem $0 $HWNDPARENT 1
  SendMessage $0 ${WM_SETTEXT} 0 "STR:&Install"
FunctionEnd

Section "Install"
  InitPluginsDir
  SetOutPath "$PLUGINSDIR\lascodia"
  File "${PAYLOAD}\setup.ps1"
  File "${PAYLOAD}\install.ps1"

  ; The native PowerShell: this installer is a 32-bit program, and 32-bit PowerShell on 64-bit
  ; Windows would see a redirected System32.
  StrCpy $1 "$SYSDIR\WindowsPowerShell\v1.0\powershell.exe"
  ${If} ${FileExists} "$WINDIR\Sysnative\WindowsPowerShell\v1.0\powershell.exe"
    StrCpy $1 "$WINDIR\Sysnative\WindowsPowerShell\v1.0\powershell.exe"
  ${EndIf}
  StrCpy $2 ""
  IfSilent 0 +2
    StrCpy $2 " -Silent"

  DetailPrint "Starting..."
  nsExec::ExecToLog '"$1" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "$PLUGINSDIR\lascodia\setup.ps1" -ExeName "$EXEFILE"$2'
  Pop $0
  SetOutPath "$TEMP"
  RMDir /r "$PLUGINSDIR\lascodia"
  ${If} $0 != "0"
    SetErrorLevel 1
    Abort "The supervisor was not installed (the reason is above)."
  ${EndIf}
SectionEnd
