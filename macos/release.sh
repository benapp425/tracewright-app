#!/bin/sh
# Build the downloadable Tracewright.app -- one binary for Apple silicon and Intel, with the engine's
# source inside (Contents/Resources/engine) -- and zip it for a GitHub release:
#
#   macos/release.sh        ->  dist/Tracewright-<version>-mac.zip
#
# On its first launch the app finds Python 3.10+ and installs its engine into ~/.tracewright-app
# (install.sh --engine-only), with the progress on screen. The app is signed ad hoc: the first time,
# people open it with right-click > Open (a Developer ID and notarization remove that step).
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
VERSION="$(sed -n 's/^version *= *"\(.*\)"/\1/p' "$ROOT/pyproject.toml" | head -1)"
DIST="$ROOT/dist"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
# the app is put together and signed outside the source folder: in a synced folder (iCloud Desktop) the file
# provider tags the bundle (com.apple.FinderInfo, fileprovider attributes) and codesign refuses it
APP="$TMP/out/Tracewright.app"
rm -rf "$DIST"
mkdir -p "$DIST" "$TMP/out"

FW="-framework Cocoa -framework WebKit -framework UserNotifications -framework UniformTypeIdentifiers -framework LocalAuthentication -framework Speech -framework AVFoundation"
for arch in arm64 x86_64; do
  swiftc -swift-version 5 -O -target "$arch-apple-macos12.0" -o "$TMP/tw-$arch" "$HERE/Tracewright.swift" $FW
done
lipo -create -output "$TMP/Tracewright" "$TMP/tw-arm64" "$TMP/tw-x86_64"
swiftc -swift-version 5 -O -o "$TMP/make_icon" "$HERE/make_icon.swift" -framework Cocoa
"$TMP/make_icon" "$TMP/AppIcon.iconset"
iconutil -c icns "$TMP/AppIcon.iconset" -o "$TMP/AppIcon.icns"

mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/engine"
cp "$TMP/Tracewright" "$APP/Contents/MacOS/Tracewright"
cp "$TMP/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"
# no built-in Python path: the app looks in ~/.tracewright-app, and installs the engine there
sed -e "s|@VERSION@|$VERSION|g" -e "s|@PYTHON@||g" -e "s|@ARCH@||g" "$HERE/Info.plist" > "$APP/Contents/Info.plist"
printf 'APPL????' > "$APP/Contents/PkgInfo"

# the engine's source, for the first-launch install
# (COPYFILE_DISABLE: no AppleDouble ._ copies of the files' extended attributes in the archive)
( cd "$ROOT" && COPYFILE_DISABLE=1 tar --exclude='__pycache__' --exclude='*.pyc' --exclude='libastar*.so' --exclude='.DS_Store' --exclude='._*' -cf - \
    tracewright pyproject.toml install.sh README.md LICENSE CHANGELOG.md THIRD_PARTY.md macos/Info.plist ) | ( cd "$APP/Contents/Resources/engine" && tar -xf - )

xattr -cr "$APP"
codesign --force --deep --sign - --identifier app.tracewright "$APP" >/dev/null
# no extended attributes or resource forks in the zip: unzip would leave a ._ file beside every file
( cd "$TMP/out" && ditto -c -k --norsrc --noextattr --keepParent Tracewright.app "$DIST/Tracewright-$VERSION-mac.zip" )
echo "Built $DIST/Tracewright-$VERSION-mac.zip ($(du -h "$DIST/Tracewright-$VERSION-mac.zip" | cut -f1))"
shasum -a 256 "$DIST/Tracewright-$VERSION-mac.zip"
