#!/usr/bin/env bash
# Build ClaudeClock.app and wrap it in a distributable .dmg.
#
#   ./packaging/make_dmg.sh
#
# Produces dist/ClaudeClock-<version>.dmg containing the app and a shortcut to
# /Applications, so installing is the usual drag-across gesture.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3)"

VERSION="$("$PYTHON" -c 'import sys; sys.path.insert(0, "src"); import claudeclock; print(claudeclock.__version__)')"
APP="dist/ClaudeClock.app"
DMG="dist/ClaudeClock-${VERSION}.dmg"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

echo "==> building ClaudeClock.app (${VERSION})"
rm -rf build dist
"$PYTHON" packaging/setup_mac.py py2app >/tmp/claudeclock-py2app.log 2>&1 || {
    echo "build failed; see /tmp/claudeclock-py2app.log" >&2
    tail -20 /tmp/claudeclock-py2app.log >&2
    exit 1
}
[ -d "$APP" ] || { echo "no app bundle produced" >&2; exit 1; }

echo "==> smoke-testing the bundle"
# Catches the classic packaging failures (missing certs, missing submodules)
# before we ship, rather than after someone downloads it.
# Written to a file first, then grepped: `grep -q` exits on the first match and
# closes the pipe, which SIGPIPEs `tee` and trips `pipefail` even on success.
"$APP/Contents/MacOS/ClaudeClock" --diagnose > /tmp/claudeclock-diagnose.log 2>&1 || true
if ! grep -q "httpx.Client  : OK" /tmp/claudeclock-diagnose.log; then
    echo "bundle smoke test failed:" >&2
    cat /tmp/claudeclock-diagnose.log >&2
    exit 1
fi
cat /tmp/claudeclock-diagnose.log

echo "==> ad-hoc signing"
# Unsigned bundles are killed outright on Apple Silicon. This is not a
# Developer ID signature - users still get Gatekeeper's first-run prompt - but
# it makes the app launchable at all.
codesign --force --deep --sign - "$APP" 2>/dev/null || \
    echo "    (codesign unavailable; the app may need a Gatekeeper override)"

echo "==> staging"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
cat > "$STAGE/README.txt" <<'TXT'
ClaudeClock
==========

Drag ClaudeClock.app to Applications, then launch it.

It appears in the menu bar as a live countdown of your Claude 5-hour usage
window. It has no Dock icon by design.

First launch: macOS will warn that the app is from an unidentified developer.
Right-click the app and choose Open, then confirm.

Requires the Claude Code CLI (`claude`) on your PATH for the automatic
session re-arm. Everything else works without it.
TXT

echo "==> creating ${DMG}"
rm -f "$DMG"
hdiutil create \
    -volname "ClaudeClock" \
    -srcfolder "$STAGE" \
    -ov -format UDZO \
    "$DMG" >/dev/null

echo "==> done"
ls -lh "$DMG"
