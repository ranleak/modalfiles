#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION="${VERSION:-0.2.0}"
DIST="dist"
mkdir -p "$DIST"

# --- MinGW-w64 cross compiler ---
compiler="${CC:-x86_64-w64-mingw32-gcc}"
if ! command -v "$compiler" >/dev/null; then
  echo "Install the MinGW-w64 cross compiler (Ubuntu: sudo apt install gcc-mingw-w64-x86-64)." >&2
  exit 1
fi

echo "==> Building legacy CLI drive-mapping installer"
"$compiler" -std=c11 -O2 -Wall -Wextra -Werror \
  -o "$DIST/ModalFiles-Setup.exe" windows/installer.c -lwininet
printf 'Built %s\n' "$PWD/$DIST/ModalFiles-Setup.exe"

echo "==> Building GUI launcher"
"$compiler" -std=c11 -O2 -Wall -Wextra -Werror -mwindows \
  -o "$DIST/ModalFilesGUI.exe" windows/gui_launcher.c -lshlwapi
printf 'Built %s\n' "$PWD/$DIST/ModalFilesGUI.exe"

# --- NSIS installer ---
makensis_bin="${MAKENSIS:-makensis}"
if command -v "$makensis_bin" >/dev/null; then
  echo "==> Building NSIS installer"
  "$makensis_bin" -DVERSION="$VERSION" -DSOURCE_DIR="$PWD" -DDIST_DIR="$PWD/$DIST" windows/ModalFiles.nsi
  printf 'Built %s\n' "$PWD/$DIST/ModalFiles-Setup-${VERSION}.exe"
else
  echo "==> Skipping NSIS installer: makensis not found" >&2
  echo "    Install NSIS (Ubuntu: sudo apt install nsis) and re-run to produce the GUI installer." >&2
fi
