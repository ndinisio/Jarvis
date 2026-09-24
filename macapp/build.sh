#!/usr/bin/env bash
# Build JARVIS.app from this folder: compile, assemble the bundle, sign it
# ad hoc (enough for your own Mac — see README.md), and say where it is.
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v swift >/dev/null; then
  echo "Swift isn't installed. Install Xcode, or just its tools: xcode-select --install" >&2
  exit 1
fi

swift build -c release
BIN="$(swift build -c release --show-bin-path)/JARVIS"

APP="build/JARVIS.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN" "$APP/Contents/MacOS/JARVIS"
cp Resources/Info.plist "$APP/Contents/Info.plist"

# The icon (Resources/AppIcon.icns — make_icon.py draws it).
if [ -f Resources/AppIcon.icns ]; then
  cp Resources/AppIcon.icns "$APP/Contents/Resources/AppIcon.icns"
fi

# Ad-hoc signature: runs on this Mac (right-click → Open the first time).
# Distributing it would need a Developer ID and notarisation.
codesign --force --deep --sign - "$APP"

echo
echo "Built $APP"
echo "Move it to /Applications (or anywhere) and open it. The first time, right-click → Open."
