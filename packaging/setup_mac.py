"""py2app build for ClaudeClock.app.

Produces a self-contained macOS bundle: its own Python runtime, no venv, no
terminal, no `pip install` on the target machine. Double-click it and it lives
in the menu bar.

    python packaging/setup_mac.py py2app

`LSUIElement` is the important flag - it makes this an accessory app, so it
gets a menu bar item and no Dock icon or app-switcher entry, which is what a
status-bar utility should be.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from setuptools import setup

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

# setuptools auto-reads any pyproject.toml in the working directory and turns
# its `dependencies` into install_requires, which py2app rejects outright.
# Building from packaging/ (which has no pyproject.toml) sidesteps that; the
# output paths below are absolute so the artefacts still land in the project.
os.chdir(Path(__file__).resolve().parent)

from claudeclock import __version__  # noqa: E402

APP = [str(ROOT / "packaging" / "app_entry.py")]

OPTIONS = {
    "argv_emulation": False,
    "iconfile": str(ROOT / "assets" / "ClaudeClock.icns"),
    "plist": {
        "CFBundleName": "ClaudeClock",
        "CFBundleDisplayName": "ClaudeClock",
        "CFBundleIdentifier": "dev.claudeclock.app",
        "CFBundleVersion": __version__,
        "CFBundleShortVersionString": __version__,
        "CFBundleExecutable": "ClaudeClock",
        # Menu bar accessory: no Dock icon, no app switcher entry.
        "LSUIElement": True,
        "LSMinimumSystemVersion": "11.0",
        "NSHumanReadableCopyright": "MIT licensed",
        "NSHighResolutionCapable": True,
        # Sending the re-arm prompt shells out to `claude`, which talks to the
        # network on our behalf; declare it rather than rely on inheritance.
        "NSAppTransportSecurity": {"NSAllowsArbitraryLoads": False},
    },
    "packages": [
        "claudeclock",
        "rumps",
        "httpx",
        "apscheduler",
        "dotenv",
        "rich",
        "certifi",
    ],
    "includes": [
        "tkinter",
        "queue",
        "logging.handlers",
        # APScheduler resolves these by entry point at runtime, so py2app's
        # static analysis cannot see them.
        "apscheduler.triggers.interval",
        "apscheduler.triggers.date",
        "apscheduler.triggers.cron",
        "apscheduler.executors.pool",
        "apscheduler.jobstores.memory",
    ],
    "excludes": [
        "pystray",       # Windows/Linux front-end; dead weight in a .app
        "PIL",
        "pytest",
        "setuptools",
        "pip",
    ],
    "strip": True,
    "dist_dir": str(ROOT / "dist"),
    "bdist_base": str(ROOT / "build"),
}

setup(
    name="ClaudeClock",
    app=APP,
    version=__version__,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
