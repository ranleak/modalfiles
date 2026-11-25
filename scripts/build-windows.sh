#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
compiler="${CC:-x86_64-w64-mingw32-gcc}"
if ! command -v "$compiler" >/dev/null; then
  echo "Install the MinGW-w64 cross compiler (Ubuntu: sudo apt install gcc-mingw-w64-x86-64)." >&2
  exit 1
fi
mkdir -p dist
"$compiler" -std=c11 -O2 -Wall -Wextra -Werror -o dist/ModalFiles-Setup.exe windows/installer.c -lwininet
printf 'Built %s\n' "$PWD/dist/ModalFiles-Setup.exe"
