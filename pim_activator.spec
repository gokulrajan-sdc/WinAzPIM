# PyInstaller spec for the Azure PIM Activator desktop UI.
#
# Build on each target platform:
#     pip install pyinstaller
#     pyinstaller pim_activator.spec
#
# Outputs:
#   Windows : dist/AzurePIMActivator/AzurePIMActivator.exe
#   Linux   : dist/AzurePIMActivator/AzurePIMActivator
#   macOS   : dist/AzurePIMActivator.app/   (BUNDLE for .dmg packaging)
#
# Cross-compilation is not supported by PyInstaller — build on the OS you target.

import sys
from PyInstaller.utils.hooks import collect_submodules

# azure-identity / msal load auth backends via dynamic import; pull them all in.
hidden = (
    collect_submodules("azure.identity")
    + collect_submodules("msal")
    + collect_submodules("msal_extensions")
)

block_cipher = None

a = Analysis(
    ["pim_ui.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AzurePIMActivator",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,        # GUI app — no console window on Windows
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="AzurePIMActivator",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="AzurePIMActivator.app",
        icon=None,
        bundle_identifier="com.azurepimactivator.app",
        info_plist={
            "CFBundleShortVersionString": "0.1.0",
            "CFBundleVersion": "0.1.0",
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
        },
    )
