"""Platform dispatch for the menu bar / tray front-end.

`sill tray` starts the monitor on background threads and then hands the main
thread to whichever native toolkit this platform uses:

* macOS   - `rumps`, giving a real menu bar item whose *title* is the countdown
* Windows - `pystray`, giving a taskbar tray icon whose image is the countdown
* Linux   - `pystray`, same as Windows where a tray is available

Missing optional dependencies are reported with the exact install command
rather than an ImportError traceback.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

from ..config import Config

log = logging.getLogger("sill.gui")


def spawn_panel() -> subprocess.Popen | None:
    """Launch the detail window as its own process.

    How to do that depends on how we are running:

    * **source checkout** - `python -m windowsill panel`
    * **PyInstaller .exe** - re-exec the executable with `--panel`
    * **py2app .app** - `open -n` a second instance of the bundle. There is no
      `python -m` inside a bundle, and launching the inner binary directly
      skips the Cocoa setup that Tkinter needs.
    """
    frozen = getattr(sys, "frozen", False)
    env = os.environ.copy()
    env["WINDOWSILL_PANEL"] = "1"

    if frozen == "macosx_app":
        # .../Windowsill.app/Contents/MacOS/Windowsill -> .../Windowsill.app
        bundle = Path(sys.executable).resolve().parents[2]
        argv = ["open", "-n", "-a", str(bundle), "--args", "--panel"]
    elif frozen:
        argv = [sys.executable, "--panel"]
    else:
        argv = [sys.executable, "-m", "windowsill", "panel"]

    try:
        return subprocess.Popen(
            argv,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
            if sys.platform.startswith("win")
            else 0,
        )
    except OSError as exc:
        log.error("could not open the detail window", extra={"error": str(exc)})
        return None


class UnsupportedFrontend(RuntimeError):
    """No usable menu bar / tray backend on this platform."""


def _install_hint(extra: str) -> str:
    return (
        f"pip install 'windowsill[{extra}]'\n"
        f"  (or: pip install {'rumps' if extra == 'macos' else 'pystray Pillow'})"
    )


def run(config: Config) -> int:
    """Start the monitor plus the platform's menu bar / tray front-end."""
    from ..monitor import Monitor

    monitor = Monitor(config, headless=True)

    if sys.platform == "darwin":
        try:
            from . import macos
        except ImportError as exc:
            raise UnsupportedFrontend(
                f"the macOS menu bar app needs rumps ({exc}).\n  {_install_hint('macos')}"
            ) from exc

        # rumps drives its own 1 Hz timer on the main thread, so the background
        # loop only needs to publish; no per-tick callback required.
        monitor.start_background()
        try:
            return macos.run(config, monitor=monitor)
        finally:
            monitor.shutdown()

    try:
        from . import tray
    except ImportError as exc:
        raise UnsupportedFrontend(
            f"the tray app needs pystray and Pillow ({exc}).\n  {_install_hint('tray')}"
        ) from exc

    app = tray.TrayApp(config, monitor=monitor)
    # pystray has no timer of its own, so the monitor's tick drives the repaint.
    monitor.start_background(on_tick=lambda _view: app.refresh())
    try:
        return app.run()
    finally:
        monitor.shutdown()


def run_panel(config: Config) -> int:
    """Open just the detail window, attaching to an already-running monitor."""
    from .panel import main as panel_main

    return panel_main(
        config.live_file,
        window_hours=config.window_hours,
        theme=config.theme,
    )
