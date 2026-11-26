; ModalFiles Windows GUI Installer
; Build on Linux: makensis -DVERSION=0.2.0 -DSOURCE_DIR=. -DDIST_DIR=dist windows/ModalFiles.nsi

!include "MUI2.nsh"
!include "FileFunc.nsh"

!ifndef VERSION
  !define VERSION "0.2.0"
!endif
!ifndef SOURCE_DIR
  !define SOURCE_DIR ".."
!endif
!ifndef DIST_DIR
  !define DIST_DIR "../dist"
!endif

!define APPNAME "ModalFiles"
!define EXE_NAME "ModalFilesGUI.exe"
!define PUBLISHER "ranleak"
!define UNINST_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}"

Name "${APPNAME}"
OutFile "${DIST_DIR}/ModalFiles-Setup-${VERSION}.exe"
InstallDir "$LOCALAPPDATA\${APPNAME}"
RequestExecutionLevel user
SetCompressor /SOLID lzma
Unicode true

!define MUI_ABORTWARNING

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "${SOURCE_DIR}/LICENSE"
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_RUN "$INSTDIR\${EXE_NAME}"
!define MUI_FINISHPAGE_RUN_TEXT "Launch ModalFiles GUI"
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "English"

!insertmacro GetSize

Section "ModalFiles GUI" SecMain
  SectionIn RO
  SetOutPath "$INSTDIR"

  ; Launcher
  File "/oname=${EXE_NAME}" "${DIST_DIR}/ModalFilesGUI.exe"

  ; Core files
  File "${SOURCE_DIR}/README.md"
  File "${SOURCE_DIR}/LICENSE"
  File "${SOURCE_DIR}/pyproject.toml"
  File "${SOURCE_DIR}/modal_app.py"

  ; Python package
  SetOutPath "$INSTDIR\modalfiles"
  File /r /x __pycache__ /x "*.pyc" /x ".pytest_cache" "${SOURCE_DIR}/modalfiles\*.*"

  ; Scripts (optional, useful for users)
  SetOutPath "$INSTDIR\scripts"
  File /nonfatal "${SOURCE_DIR}/scripts\build-windows.sh"

  SetOutPath "$INSTDIR"

  ; Uninstaller
  WriteUninstaller "$INSTDIR\Uninstall.exe"

  ; Start Menu shortcuts
  CreateDirectory "$SMPROGRAMS\${APPNAME}"
  CreateShortcut "$SMPROGRAMS\${APPNAME}\${APPNAME}.lnk" "$INSTDIR\${EXE_NAME}" "" "$INSTDIR\${EXE_NAME}" 0
  CreateShortcut "$SMPROGRAMS\${APPNAME}\Uninstall ${APPNAME}.lnk" "$INSTDIR\Uninstall.exe"

  ; Desktop shortcut
  CreateShortcut "$DESKTOP\${APPNAME}.lnk" "$INSTDIR\${EXE_NAME}" "" "$INSTDIR\${EXE_NAME}" 0

  ; Registry (per-user uninstall)
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayName" "${APPNAME}"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${UNINST_KEY}" "Publisher" "${PUBLISHER}"
  WriteRegStr HKCU "${UNINST_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINST_KEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayIcon" "$INSTDIR\${EXE_NAME}"
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoRepair" 1

  ; Estimated size
  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  IntFmt $0 "0x%08X" $0
  WriteRegDWORD HKCU "${UNINST_KEY}" "EstimatedSize" "$0"
SectionEnd

Section "Uninstall"
  Delete "$INSTDIR\${EXE_NAME}"
  Delete "$INSTDIR\Uninstall.exe"
  Delete "$INSTDIR\README.md"
  Delete "$INSTDIR\LICENSE"
  Delete "$INSTDIR\pyproject.toml"
  Delete "$INSTDIR\modal_app.py"

  RMDir /r "$INSTDIR\modalfiles"
  RMDir /r "$INSTDIR\scripts"
  RMDir "$INSTDIR"

  Delete "$SMPROGRAMS\${APPNAME}\${APPNAME}.lnk"
  Delete "$SMPROGRAMS\${APPNAME}\Uninstall ${APPNAME}.lnk"
  RMDir "$SMPROGRAMS\${APPNAME}"
  Delete "$DESKTOP\${APPNAME}.lnk"

  DeleteRegKey HKCU "${UNINST_KEY}"
SectionEnd
