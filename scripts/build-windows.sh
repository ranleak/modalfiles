#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION="${VERSION:-0.2.0}"
PYTHON_VERSION="${PYTHON_VERSION:-3.14.10}"
PYTHON_EMBED_ARCH="${PYTHON_EMBED_ARCH:-amd64}"
PYTHON_EMBED_URL="${PYTHON_EMBED_URL:-https://www.python.org/ftp/python/${PYTHON_VERSION}/python-${PYTHON_VERSION}-embed-${PYTHON_EMBED_ARCH}.zip}"
PYTHON_EMBED_ZIP="${PYTHON_EMBED_ZIP:-}"
CACHE_DIR="${CACHE_DIR:-.cache/windows}"
DIST="dist"
BUNDLE_DIR="$DIST/ModalFilesGUI"
mkdir -p "$DIST"

# Known official SHA-256 digests for python.org Windows amd64 embeddable zips.
default_embed_sha256() {
  case "${1}:${2}" in
    3.14.10:amd64) echo "90905e64a840557d8c1bc4f5b74db3d7ee85e3ffb217611cac1c7813acea4159" ;;
    3.13.17:amd64) echo "9e35319bffedab8b36ad44d8f377cb4f1733bcec257a0b6d04d94f01486ddb38" ;;
    3.12.10:amd64) echo "4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3" ;;
    3.15.2:amd64)  echo "d012f3aa29cf07436dae8aea1fec0fd195c2612a0b059b1f915ae0afb9ad1f62" ;;
    *) echo "" ;;
  esac
}

if [[ -z "${PYTHON_EMBED_SHA256+x}" ]]; then
  if [[ -z "$PYTHON_EMBED_ZIP" ]]; then
    PYTHON_EMBED_SHA256="$(default_embed_sha256 "$PYTHON_VERSION" "$PYTHON_EMBED_ARCH")"
  else
    PYTHON_EMBED_SHA256=""
  fi
fi

verify_sha256() {
  local file="$1" expected="$2"
  python3 - "$file" "$expected" <<'PY'
import hashlib, sys
path, expected = sys.argv[1], sys.argv[2].lower()
h = hashlib.sha256()
with open(path, 'rb') as f:
    for chunk in iter(lambda: f.read(1024 * 1024), b''):
        h.update(chunk)
actual = h.hexdigest()
if actual != expected:
    raise SystemExit(f"SHA-256 mismatch for {path}: expected {expected}, got {actual}")
print(f"Verified SHA-256: {actual}")
PY
}

download_file() {
  local url="$1" out="$2"
  local tmp="${out}.tmp.$$"
  mkdir -p "$(dirname "$out")"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL --retry 3 -o "$tmp" "$url"
  elif command -v wget >/dev/null 2>&1; then
    wget -qO "$tmp" "$url"
  else
    python3 - "$url" "$tmp" <<'PY'
import shutil, sys, urllib.request
with urllib.request.urlopen(sys.argv[1], timeout=60) as r, open(sys.argv[2], 'wb') as f:
    shutil.copyfileobj(r, f)
PY
  fi
  mv -f "$tmp" "$out"
}

# --- MinGW-w64 cross compiler ---
compiler="${CC:-x86_64-w64-mingw32-gcc}"
if ! command -v "$compiler" >/dev/null 2>&1; then
  echo "Install the MinGW-w64 cross compiler (Ubuntu: sudo apt install gcc-mingw-w64-x86-64)." >&2
  exit 1
fi

echo "==> Building legacy CLI drive-mapping installer"
"$compiler" -std=c11 -O2 -Wall -Wextra -Werror \
  -o "$DIST/ModalFiles-Setup.exe" windows/installer.c -lwininet
printf 'Built %s\n' "$PWD/$DIST/ModalFiles-Setup.exe"

echo "==> Building GUI launcher"
"$compiler" -std=c11 -O2 -Wall -Wextra -Werror -mwindows \
  -o "$DIST/ModalFilesGUI.exe" windows/gui_launcher.c
printf 'Built %s\n' "$PWD/$DIST/ModalFilesGUI.exe"

# --- Bundled miniature Python runtime (embeddable distribution) ---
if [[ -n "$PYTHON_EMBED_ZIP" ]]; then
  embed_zip="$PYTHON_EMBED_ZIP"
  if [[ ! -f "$embed_zip" ]]; then
    echo "PYTHON_EMBED_ZIP not found: $embed_zip" >&2
    exit 1
  fi
  echo "==> Using local Python embeddable zip: $embed_zip"
else
  embed_zip="$CACHE_DIR/python-${PYTHON_VERSION}-embed-${PYTHON_EMBED_ARCH}.zip"
  if [[ ! -f "$embed_zip" ]]; then
    echo "==> Downloading Python ${PYTHON_VERSION} (${PYTHON_EMBED_ARCH}) embeddable package"
    download_file "$PYTHON_EMBED_URL" "$embed_zip"
  else
    echo "==> Using cached Python embeddable zip: $embed_zip"
  fi
fi

if [[ -n "$PYTHON_EMBED_SHA256" ]]; then
  if ! verify_sha256 "$embed_zip" "$PYTHON_EMBED_SHA256"; then
    if [[ -z "$PYTHON_EMBED_ZIP" ]]; then
      rm -f "$embed_zip"
    fi
    exit 1
  fi
elif [[ -z "$PYTHON_EMBED_ZIP" && "${ALLOW_UNVERIFIED_PYTHON:-0}" != "1" ]]; then
  echo "No pinned SHA-256 for Python ${PYTHON_VERSION} (${PYTHON_EMBED_ARCH}); set PYTHON_EMBED_SHA256 or ALLOW_UNVERIFIED_PYTHON=1." >&2
  exit 1
fi

echo "==> Staging portable GUI bundle ($BUNDLE_DIR)"
rm -rf "$BUNDLE_DIR"
mkdir -p "$BUNDLE_DIR"
cp "$DIST/ModalFilesGUI.exe" "$BUNDLE_DIR/ModalFilesGUI.exe"
python3 scripts/prepare-python-embed.py "$embed_zip" "$BUNDLE_DIR/python"

python3 - "$PWD" "$PWD/$BUNDLE_DIR" <<'PY'
import shutil, sys
from pathlib import Path

src_root = Path(sys.argv[1])
dst_root = Path(sys.argv[2])

def ignore(_dir, names):
    return {n for n in names if n in ('__pycache__', '.pytest_cache') or n.endswith('.pyc')}

shutil.copytree(src_root / 'modalfiles', dst_root / 'modalfiles', ignore=ignore)
for name in ('README.md', 'LICENSE', 'pyproject.toml', 'modal_app.py'):
    shutil.copy2(src_root / name, dst_root / name)
PY

portable_zip="$DIST/ModalFilesGUI-${VERSION}-win64.zip"
python3 - "$PWD/$BUNDLE_DIR" "$PWD/$portable_zip" <<'PY'
import sys, zipfile
from pathlib import Path

bundle = Path(sys.argv[1])
out_zip = Path(sys.argv[2])
with zipfile.ZipFile(out_zip, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
    for p in sorted(bundle.rglob('*')):
        if p.is_file():
            arcname = Path(bundle.name) / p.relative_to(bundle)
            zf.write(p, arcname.as_posix())
PY
printf 'Built %s\n' "$PWD/$portable_zip"

# --- NSIS installer ---
makensis_bin="${MAKENSIS:-makensis}"
if command -v "$makensis_bin" >/dev/null 2>&1; then
  echo "==> Building NSIS installer"
  "$makensis_bin" \
    -DVERSION="$VERSION" \
    -DSOURCE_DIR="$PWD" \
    -DDIST_DIR="$PWD/$DIST" \
    -DPYTHON_EMBED_DIR="$PWD/$BUNDLE_DIR/python" \
    windows/ModalFiles.nsi
  printf 'Built %s\n' "$PWD/$DIST/ModalFiles-Setup-${VERSION}.exe"
else
  echo "==> Skipping NSIS installer: makensis not found" >&2
  echo "    Install NSIS (Ubuntu: sudo apt install nsis) and re-run to produce the GUI installer." >&2
fi
