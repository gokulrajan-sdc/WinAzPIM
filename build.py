#!/usr/bin/env python3
"""
Build the AzurePIMActivator desktop app for the current platform.

Runs PyInstaller against pim_activator.spec, then wraps the output:
  - Windows : zips dist/AzurePIMActivator into AzurePIMActivator-windows.zip
  - macOS   : creates AzurePIMActivator.dmg from dist/AzurePIMActivator.app
  - Linux   : tars dist/AzurePIMActivator into AzurePIMActivator-linux.tar.gz

Cross-compilation is not supported — run this on each target OS.

Prereqs:
    pip install -r requirements-dev.txt
"""

import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
BUILD = ROOT / "build"
ARTIFACTS = ROOT / "artifacts"


def run(cmd, **kw):
    print(f"$ {' '.join(cmd)}", flush=True)
    subprocess.run(cmd, check=True, **kw)


def clean():
    for d in (DIST, BUILD):
        if d.exists():
            shutil.rmtree(d)
    ARTIFACTS.mkdir(exist_ok=True)


def run_pyinstaller():
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "pim_activator.spec"])


def package_windows():
    src = DIST / "AzurePIMActivator"
    if not src.exists():
        sys.exit(f"Expected PyInstaller output at {src}, but it's missing.")
    out = ARTIFACTS / "AzurePIMActivator-windows.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in src.rglob("*"):
            zf.write(path, path.relative_to(src.parent))
    print(f"✓ {out}  ({out.stat().st_size // (1024*1024)} MiB)")


def package_linux():
    src = DIST / "AzurePIMActivator"
    if not src.exists():
        sys.exit(f"Expected PyInstaller output at {src}, but it's missing.")
    out = ARTIFACTS / "AzurePIMActivator-linux.tar.gz"
    with tarfile.open(out, "w:gz") as tf:
        tf.add(src, arcname="AzurePIMActivator")
    print(f"✓ {out}  ({out.stat().st_size // (1024*1024)} MiB)")


def package_macos():
    app = DIST / "AzurePIMActivator.app"
    if not app.exists():
        sys.exit(f"Expected .app bundle at {app}, but it's missing.")
    out = ARTIFACTS / "AzurePIMActivator-macos.dmg"
    if out.exists():
        out.unlink()
    # hdiutil ships with macOS — no third-party tooling required.
    run([
        "hdiutil", "create",
        "-volname", "AzurePIMActivator",
        "-srcfolder", str(app),
        "-ov",
        "-format", "UDZO",
        str(out),
    ])
    print(f"✓ {out}  ({out.stat().st_size // (1024*1024)} MiB)")


def main():
    clean()
    run_pyinstaller()
    if sys.platform.startswith("win"):
        package_windows()
    elif sys.platform == "darwin":
        package_macos()
    elif sys.platform.startswith("linux"):
        package_linux()
    else:
        sys.exit(f"Unsupported platform: {sys.platform}")


if __name__ == "__main__":
    main()
