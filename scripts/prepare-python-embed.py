#!/usr/bin/env python3
"""Stage the Windows embeddable Python runtime for the ModalFiles GUI bundle.

Extracts a ``python-*-embed-amd64.zip`` (from python.org) into a staging
directory and patches its ``python*._pth`` file so the bundled interpreter can
import the ``modalfiles`` package from the install directory (the parent of
the ``python\\`` folder).

The GUI server (``modalfiles/gui/server.py``) is stdlib-only, so the
embeddable package -- which ships without ``site``/``pip``/site-packages --
is sufficient. No third-party packages are installed into the bundled
runtime.

Usage:
    python3 scripts/prepare-python-embed.py <embed.zip> <dest-dir>

The destination directory is recreated cleanly. On success the directory
contains ``pythonw.exe``, ``python.exe``, ``python3xx.dll``, ``python3xx.zip``,
``vcruntime*.dll``, and a patched ``python3xx._pth``.
"""
from __future__ import annotations

import shutil
import sys
import zipfile
from pathlib import Path


def patch_pth(pth_path: Path) -> list[str]:
    """Ensure the ._pth file can import the install dir and keeps site off.

    Adds ``..`` (the install directory, parent of the ``python\\`` folder)
    after the existing ``.`` entry if missing, and comments out any bare
    ``import site`` line so user/system site-packages stay isolated.
    Returns the resulting lines.
    """
    raw = pth_path.read_text(encoding="utf-8", errors="replace")
    lines = raw.splitlines()
    stripped = [ln.strip() for ln in lines]

    out: list[str] = []
    has_parent = ".." in stripped
    for ln in lines:
        s = ln.strip()
        if s == "import site":
            out.append("# import site")
            continue
        out.append(ln)
        if s == "." and not has_parent:
            out.append("..")
            has_parent = True
    if not has_parent:
        out.append("..")

    pth_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return out


def _safe_extract_all(zf: zipfile.ZipFile, dest: Path) -> None:
    dest_resolved = dest.resolve()
    for info in zf.infolist():
        name = info.filename
        if not name:
            continue
        member_path = (dest_resolved / name).resolve()
        if member_path != dest_resolved and dest_resolved not in member_path.parents:
            raise SystemExit(f"unsafe path in embeddable zip: {name!r}")
    zf.extractall(dest_resolved)


def prepare(zip_path: Path, dest: Path) -> Path:
    """Extract *zip_path* into *dest*, patch the ._pth file, verify layout."""
    if not zip_path.is_file():
        raise SystemExit(f"embeddable zip not found: {zip_path}")

    dest_resolved = dest.resolve()
    if dest_resolved == Path(dest_resolved.anchor) or dest_resolved == Path.cwd().resolve():
        raise SystemExit(f"refusing to clear unsafe destination path: {dest}")

    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path) as zf:
        _safe_extract_all(zf, dest)

    pth_files = sorted(dest.glob("python*._pth"))
    if len(pth_files) != 1:
        raise SystemExit(
            f"expected exactly one python*._pth in {dest}, "
            f"found {[p.name for p in pth_files]}"
        )
    pth = pth_files[0]
    stem = pth.name[: -len("._pth")]  # e.g. "python314"
    lines = patch_pth(pth)

    required = [
        "pythonw.exe",
        "python.exe",
        f"{stem}.dll",
        f"{stem}.zip",
    ]
    missing = [name for name in required if not (dest / name).is_file()]
    if not any(dest.glob("vcruntime*.dll")):
        missing.append("vcruntime*.dll")
    if missing:
        raise SystemExit(
            f"staged runtime in {dest} is missing required files: "
            + ", ".join(missing)
        )

    print(f"staged {stem} embeddable runtime in {dest}")
    print(f"{pth.name}:")
    for ln in lines:
        print(f"  {ln}")
    return dest


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__.strip().splitlines()[0], file=sys.stderr)
        print("usage: prepare-python-embed.py <embed.zip> <dest-dir>", file=sys.stderr)
        return 2
    prepare(Path(argv[1]), Path(argv[2]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
