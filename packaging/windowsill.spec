# PyInstaller spec for the Windows build.
#
#   pip install pyinstaller
#   pyinstaller packaging/windowsill.spec
#
# Produces dist/Windowsill.exe - a single self-contained file with its own
# Python runtime. `console=False` is what keeps a terminal window from
# appearing behind the tray icon.
#
# Must be built on Windows: PyInstaller does not cross-compile.

import sys
from pathlib import Path

ROOT = Path(SPECPATH).parent
sys.path.insert(0, str(ROOT / "src"))

block_cipher = None

datas = []

# certifi ships its CA bundle as package data. Without it every HTTPS call
# fails at runtime with a bare FileNotFoundError - the same class of bug the
# macOS build hit, so it is worth being explicit on both platforms.
try:
    import certifi

    datas.append((certifi.where(), "certifi"))
except ImportError:
    pass

a = Analysis(
    [str(ROOT / "packaging" / "app_entry.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "windowsill",
        "windowsill.gui.tray",
        "windowsill.gui.panel",
        "pystray._win32",
        "PIL._tkinter_finder",
        # APScheduler resolves these by entry point at runtime, so static
        # analysis cannot see them.
        "apscheduler.triggers.interval",
        "apscheduler.triggers.date",
        "apscheduler.triggers.cron",
        "apscheduler.executors.pool",
        "apscheduler.jobstores.memory",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "rumps",       # macOS front-end
        "AppKit",
        "Foundation",
        "objc",
        "pytest",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="Windowsill",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # no terminal window behind the tray icon
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "assets" / "windowsill.ico"),
)
