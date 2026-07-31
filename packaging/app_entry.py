"""Entry point for the bundled desktop app.

Distinct from the `cclock` console script because a double-clicked app has
different needs: no terminal to print a traceback into, no shell PATH, and no
argv worth parsing. It goes straight to the menu bar / tray front-end and
routes any startup failure to a visible dialog rather than a silent exit.
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path


def _ensure_path() -> None:
    """Make the package importable when run from a source checkout."""
    here = Path(__file__).resolve().parent
    src = here.parent / "src"
    if src.is_dir() and str(src) not in sys.path:
        sys.path.insert(0, str(src))


def _fix_path_for_bundle() -> None:
    """Give the bundle a usable PATH.

    A GUI app launched from Finder inherits a minimal PATH that does not
    include Homebrew or `~/.local/bin`, so the `claude` CLI the re-arm step
    shells out to would be invisible. Login shells get it right, so borrow
    their PATH; fall back to appending the usual locations.
    """
    if not sys.platform == "darwin":
        return

    candidates = [
        str(Path.home() / ".local" / "bin"),
        str(Path.home() / ".bun" / "bin"),
        "/opt/homebrew/bin",
        "/usr/local/bin",
    ]

    try:
        import subprocess

        shell = os.environ.get("SHELL", "/bin/zsh")
        result = subprocess.run(
            [shell, "-l", "-c", "echo $PATH"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            os.environ["PATH"] = result.stdout.strip()
            return
    except (OSError, subprocess.SubprocessError):
        pass

    existing = os.environ.get("PATH", "").split(os.pathsep)
    os.environ["PATH"] = os.pathsep.join(
        [*existing, *[c for c in candidates if c not in existing]]
    )


def _fix_ssl_certificates() -> None:
    """Point OpenSSL at the CA bundle inside the app.

    `certifi.where()` resolves through importlib.resources, which does not
    reliably produce an on-disk path inside a frozen bundle - the result is a
    `FileNotFoundError` the first time anything opens an HTTPS connection.
    httpx checks `SSL_CERT_FILE` before falling back to certifi, so setting it
    to the PEM we actually shipped fixes every client at once.
    """
    if not getattr(sys, "frozen", False):
        return

    # py2app's bootstrap points SSL_CERT_FILE at a deliberate placeholder -
    # `Contents/Resources/openssl.ca/no-such-file` - to stop OpenSSL falling
    # back to the build machine's CA store. So the test is not "is it set" but
    # "does it point at a file that exists"; httpx trusts the variable blindly
    # and raises FileNotFoundError on the first HTTPS call otherwise.
    current = os.environ.get("SSL_CERT_FILE")
    if current and Path(current).is_file():
        return

    try:
        import certifi

        where = Path(certifi.where())
        if where.is_file():
            os.environ["SSL_CERT_FILE"] = str(where)
            return
    except Exception:
        pass

    # Fall back to locating the PEM inside the bundle ourselves.
    roots = [
        Path(sys.executable).resolve().parents[1] / "Resources",
        Path(getattr(sys, "_MEIPASS", "")) if hasattr(sys, "_MEIPASS") else None,
    ]
    for root in roots:
        if not root or not root.is_dir():
            continue
        for pem in root.rglob("cacert.pem"):
            os.environ["SSL_CERT_FILE"] = str(pem)
            return


def _report(message: str, detail: str = "") -> None:
    """Surface a fatal error where a windowless app can actually show it."""
    sys.stderr.write(f"{message}\n{detail}\n")
    if sys.platform == "darwin":
        try:
            import subprocess

            body = (message + ("\n\n" + detail if detail else ""))[:900]
            subprocess.run(
                ["osascript", "-e",
                 'display dialog {} with title "ClaudeClock" buttons {{"OK"}} '
                 'default button 1 with icon caution'.format(
                     _applescript_string(body))],
                capture_output=True, timeout=60, check=False,
            )
        except Exception:
            pass


def _applescript_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def _diagnose() -> int:
    """Print what the bundle resolved at runtime.

    A packaged app has no terminal, so when it fails to start there is nothing
    to inspect. `ClaudeClock.app/Contents/MacOS/ClaudeClock --diagnose` prints
    the environment the app actually sees.
    """
    print(f"frozen        : {getattr(sys, 'frozen', None)}")
    print(f"executable    : {sys.executable}")
    print(f"cwd           : {os.getcwd()}")
    print(f"SSL_CERT_FILE : {os.environ.get('SSL_CERT_FILE')}")
    pem = os.environ.get("SSL_CERT_FILE")
    print(f"  exists      : {bool(pem) and Path(pem).is_file()}")
    try:
        import certifi

        where = certifi.where()
        print(f"certifi.where : {where}")
        print(f"  exists      : {Path(where).is_file()}")
    except Exception as exc:
        print(f"certifi       : FAILED {exc!r}")
    try:
        import httpx

        httpx.Client().close()
        print("httpx.Client  : OK")
    except Exception as exc:
        print(f"httpx.Client  : FAILED {type(exc).__name__}: {exc}")
    import shutil

    print(f"claude on PATH: {shutil.which('claude')}")
    return 0


def main() -> int:
    _ensure_path()
    _fix_path_for_bundle()
    _fix_ssl_certificates()

    if "--diagnose" in sys.argv:
        return _diagnose()

    try:
        from claudeclock.config import Config, ConfigError
        from claudeclock.logging_setup import install_excepthook, setup_logging
    except Exception:
        _report("ClaudeClock could not start.", traceback.format_exc())
        return 1

    try:
        config = Config.load()
        config.ensure_state_dir()
    except ConfigError as exc:
        _report("Configuration problem", str(exc))
        return 2

    logger = setup_logging(
        level=config.log_level,
        log_file=config.log_file,
        json_logs=config.log_json,
        max_bytes=config.log_max_bytes,
        backup_count=config.log_backup_count,
        console=False,
    )
    install_excepthook(logger)

    # `cclock panel` is spawned as a subprocess by the front-ends. Inside a
    # bundle there is no `python -m claudeclock` to call, so the app re-execs
    # itself with this marker instead.
    if os.environ.get("CLAUDECLOCK_PANEL") == "1" or "--panel" in sys.argv:
        from claudeclock.gui.app import run_panel

        return run_panel(config)

    try:
        from claudeclock.gui.app import UnsupportedFrontend, run

        return run(config)
    except UnsupportedFrontend as exc:
        _report("ClaudeClock cannot show a menu bar item", str(exc))
        return 2
    except Exception:
        logger.exception("fatal error in the menu bar app")
        _report("ClaudeClock stopped unexpectedly.", traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
