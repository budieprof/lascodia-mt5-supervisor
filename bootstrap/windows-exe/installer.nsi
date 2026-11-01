; Lascodia supervisor - double-click Windows installer ("Lascodia Setup <code>.exe").
;
; Built on any machine with NSIS (makensis) by tools/build_exe.sh, which passes:
;   /DVERSION=1.0.0  /DOUTFILE=...exe  /DPAYLOAD=<folder with setup.ps1 + the engine's install.ps1>
;
; It installs nothing by itself: it unpacks setup.ps1 and install.ps1 into a private temp folder and
; runs setup.ps1 with the .exe's own file name (the install code is in it; when the file was renamed
; an "Install code" page asks for it), for the person signed in to Windows - no administrator rights needed (the supervisor runs in that person's session, as
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
!include "nsDialogs.nsh"
!include "StrFunc.nsh"
${StrStr}

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

Var NeedCode      ; 1 when the file name carries no install code (the file was renamed)
Var CodeField

VIProductVersion "${VERSION}.0"
VIAddVersionKey "ProductName" "Lascodia Supervisor setup"
VIAddVersionKey "FileDescription" "Connects this computer's MetaTrader 5 terminals to Lascodia"
VIAddVersionKey "CompanyName" "Lascodia"
VIAddVersionKey "LegalCopyright" "Lascodia"
VIAddVersionKey "FileVersion" "${VERSION}"
VIAddVersionKey "ProductVersion" "${VERSION}"

!define MUI_ABORTWARNING
!define MUI_WELCOMEPAGE_TITLE "Connect this computer to Lascodia"
!define MUI_WELCOMEPAGE_TEXT "This installs the Lascodia supervisor, a small helper that runs in the background and looks after the MetaTrader 5 terminals on this computer.$\r$\n$\r$\nIt is installed for you (the person signed in to Windows) and starts every time you sign in. It finds your MetaTrader 5 terminals and connects them to your organisation. It does not change your terminals or anything else on this computer.$\r$\n$\r$\nIt takes about a minute. Keep the console page open: it shows this computer connecting."
!define MUI_PAGE_CUSTOMFUNCTION_SHOW WelcomeShow
!insertmacro MUI_PAGE_WELCOME
Page custom CodePage CodePageLeave
!define MUI_PAGE_HEADER_TEXT "Connecting this computer"
!define MUI_PAGE_HEADER_SUBTEXT "Installing the Lascodia supervisor and connecting it to your organisation..."
!define MUI_INSTFILESPAGE_FINISHHEADER_TEXT "Connected"
!define MUI_INSTFILESPAGE_FINISHHEADER_SUBTEXT "The supervisor is installed and connected."
!define MUI_INSTFILESPAGE_ABORTHEADER_TEXT "Not installed"
!define MUI_INSTFILESPAGE_ABORTHEADER_SUBTEXT "The supervisor was not installed. The reason is in the list below."
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_TITLE "This computer is connected"
!define MUI_FINISHPAGE_TEXT "The Lascodia supervisor is running and connected to your organisation.$\r$\n$\r$\nGo back to the console to finish: it lists the MetaTrader 5 terminals it found and anything still to do.$\r$\n$\r$\nYou can delete this installer now."
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_LANGUAGE "English"

; The welcome page's button says Install when the next page installs (the code is in the name).
Function WelcomeShow
  ${If} $NeedCode == 0
    GetDlgItem $0 $HWNDPARENT 1
    SendMessage $0 ${WM_SETTEXT} 0 "STR:&Install"
  ${EndIf}
FunctionEnd

; Only when the file was renamed: ask for the install code (or the whole install line).  All
; dialogs live in this window: setup.ps1 runs hidden and must never open one.
Function CodePage
  ${If} $NeedCode == 0
    Abort
  ${EndIf}
  !insertmacro MUI_HEADER_TEXT "Install code" "This installer's file name does not carry its install code."
  nsDialogs::Create 1018
  Pop $0
  ${NSD_CreateLabel} 0 0 100% 48u "The console names the installer after its install code, and this copy was renamed.$\r$\n$\r$\nIn the console open Terminals, click Add a computer, copy the install line (or just the code starting with lsc_enr_) and paste it here."
  Pop $0
  ${NSD_CreateText} 0 56u 100% 13u ""
  Pop $CodeField
  ${NSD_SetFocus} $CodeField
  GetDlgItem $0 $HWNDPARENT 1
  SendMessage $0 ${WM_SETTEXT} 0 "STR:&Install"
  nsDialogs::Show
FunctionEnd

Function CodePageLeave
  ${NSD_GetText} $CodeField $0
  ${StrStr} $1 $0 "lsc_enr_"
  ${If} $1 == ""
    MessageBox MB_ICONEXCLAMATION|MB_OK "That is not an install code. It starts with lsc_enr_ - copy it from Add a computer in the console."
    Abort
  ${EndIf}
  ; setup.ps1 picks the code out of it (exact format check there); an environment variable needs no quoting.
  System::Call 'kernel32::SetEnvironmentVariable(t "LASCODIA_EXE_TYPED", t r0)'
FunctionEnd

; The two scripts are unpacked before any page shows, so the progress list carries only the
; supervisor's own words.  $PLUGINSDIR (a private temp folder) is deleted when the installer exits.
Function .onInit
  InitPluginsDir
  SetOutPath "$PLUGINSDIR\lascodia"
  File "${PAYLOAD}\setup.ps1"
  File "${PAYLOAD}\install.ps1"
  ${StrStr} $0 $EXEFILE "lsc_enr_"
  StrCpy $NeedCode 0
  ${If} $0 == ""
    StrCpy $NeedCode 1
  ${EndIf}
FunctionEnd

Section "Install"

  ; The native PowerShell: this installer is a 32-bit program, and 32-bit PowerShell on 64-bit
  ; Windows would see a redirected System32.
  StrCpy $1 "$SYSDIR\WindowsPowerShell\v1.0\powershell.exe"
  ${If} ${FileExists} "$WINDIR\Sysnative\WindowsPowerShell\v1.0\powershell.exe"
    StrCpy $1 "$WINDIR\Sysnative\WindowsPowerShell\v1.0\powershell.exe"
  ${EndIf}
  nsExec::ExecToLog '"$1" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "$PLUGINSDIR\lascodia\setup.ps1" -ExeName "$EXEFILE"'
  Pop $0
  ${If} $0 != "0"
    SetErrorLevel 1
    ; The reason setup.ps1 left (UTF-16, a few lines), in front of this window; skipped with /S.
    StrCpy $3 ""
    ClearErrors
    FileOpen $4 "$PLUGINSDIR\lascodia\failure.txt" r
    ${IfNot} ${Errors}
      ${For} $5 1 6
        ClearErrors
        FileReadUTF16LE $4 $6
        ${If} ${Errors}
          ${Break}
        ${EndIf}
        StrCpy $3 "$3$6"
      ${Next}
      FileClose $4
    ${EndIf}
    ${If} $3 == ""
      StrCpy $3 "The installer stopped without saying why. The list in this window shows how far it got."
    ${EndIf}
    MessageBox MB_ICONSTOP|MB_OK "$3" /SD IDOK
    Abort "The supervisor was not installed (the reason is in the list)."
  ${EndIf}
SectionEnd
