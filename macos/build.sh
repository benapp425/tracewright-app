#!/bin/sh
# Build Tracewright.app: the native macOS window around the Tracewright engine (a Python server).
# Needs the Swift compiler (Xcode or its Command Line Tools: xcode-select --install).
#
#   macos/build.sh APP_PATH PYTHON [ARCH]
#
# PYTHON is the engine's interpreter (the install's venv); ARCH the CPU architecture its native
# packages were installed for (arm64 or x86_64; default: this machine's).
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
APP="$1"
PY="$2"
ARCH="${3:-$(uname -m)}"
if [ -z "$APP" ] || [ -z "$PY" ]; then
  echo "usage: $0 APP_PATH PYTHON [ARCH]" >&2
  exit 2
fi
VERSION="$(sed -n 's/^version *= *"\(.*\)"/\1/p' "$HERE/../pyproject.toml" | head -1)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

swiftc -swift-version 5 -O -target "$ARCH-apple-macos12.0" -o "$TMP/Tracewright" "$HERE/Tracewright.swift" \
  -framework Cocoa -framework WebKit -framework UserNotifications -framework UniformTypeIdentifiers -framework LocalAuthentication -framework Speech -framework AVFoundation
swiftc -swift-version 5 -O -o "$TMP/make_icon" "$HERE/make_icon.swift" -framework Cocoa
"$TMP/make_icon" "$TMP/AppIcon.iconset"
iconutil -c icns "$TMP/AppIcon.iconset" -o "$TMP/AppIcon.icns"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$TMP/Tracewright" "$APP/Contents/MacOS/Tracewright"
cp "$TMP/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"
sed -e "s|@VERSION@|$VERSION|g" -e "s|@PYTHON@|$PY|g" -e "s|@ARCH@|$ARCH|g" "$HERE/Info.plist" > "$APP/Contents/Info.plist"
printf 'APPL????' > "$APP/Contents/PkgInfo"
# ad hoc signature: enough to run on this Mac (sharing it needs a Developer ID and notarization)
codesign --force --sign - --identifier app.tracewright "$APP" >/dev/null
LSREG=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister
[ -x "$LSREG" ] && "$LSREG" -f "$APP" >/dev/null 2>&1 || true
touch "$APP"
echo "Built $APP ($VERSION, $ARCH)"
