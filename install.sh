#!/bin/sh
# Install Tracewright for this user: a private Python environment, the `tracewright` command, and
# (on macOS) Tracewright.app in ~/Applications. Re-run to update.
#
#   ./install.sh               install / update
#   ./install.sh --engine-only the engine and the command, not the app (a downloaded Tracewright.app runs this)
#   ./install.sh --check       what this Mac has (JSON): Python 3.10+, KiCad
#   ./install.sh --uninstall
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
PREFIX="${TRACEWRIGHT_PREFIX:-$HOME/.tracewright-app}"
VENV="$PREFIX/venv"
BIN="$HOME/.local/bin"
export PATH="/opt/homebrew/bin:/usr/local/bin:/Library/Frameworks/Python.framework/Versions/Current/bin:$PATH"

find_python() {
  for c in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
      command -v "$c"; return 0
    fi
  done
  return 1
}

if [ "$1" = "--check" ]; then
  P="$(find_python || true)"
  V=""; [ -n "$P" ] && V="$("$P" -c 'import platform; print(platform.python_version())')"
  K=""; for k in /Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli "$(command -v kicad-cli 2>/dev/null)"; do [ -n "$k" ] && [ -x "$k" ] && K="$k" && break; done
  printf '{"python": "%s", "version": "%s", "kicad": "%s", "installed": %s}\n' "$P" "$V" "$K" "$([ -x "$VENV/bin/python" ] && echo true || echo false)"
  exit 0
fi

if [ "$1" = "--uninstall" ]; then
  rm -rf "$PREFIX" "$BIN/tracewright" "$HOME/Applications/Tracewright.app"
  echo "Removed the app. Your projects and settings are kept (projects: ~/Documents/Tracewright Projects;"
  echo "settings and lessons: ~/Library/Application Support/Tracewright on macOS)."
  exit 0
fi

PY="$(find_python || true)"
if [ -z "$PY" ]; then
  echo "Tracewright needs Python 3.10 or newer (python.org or 'brew install python')." >&2
  exit 1
fi
echo "Python:  $PY ($($PY --version 2>&1))"

mkdir -p "$PREFIX" "$BIN"
if [ ! -x "$VENV/bin/python" ]; then
  "$PY" -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install --quiet --upgrade pip
# pip builds in the source folder: from inside a downloaded app, build from a copy (the app stays untouched)
SRC="$HERE"
if [ "$1" = "--engine-only" ] || [ ! -w "$HERE" ]; then
  SRC="$(mktemp -d)/tracewright-src"
  cp -R "$HERE" "$SRC"
fi
"$VENV/bin/python" -m pip install --quiet --upgrade "$SRC"
[ "$SRC" != "$HERE" ] && rm -rf "$(dirname "$SRC")"
echo "Installed into $VENV"

# pip installed native wheels (numpy, Pillow, ...) for this shell's CPU architecture. Run Python in
# that same architecture everywhere: macOS starts a script-based .app under Rosetta on Apple silicon.
RUN=""
if [ "$(uname)" = "Darwin" ]; then
  ARCH="$("$VENV/bin/python" -c 'import platform; print(platform.machine())')"
  case "$ARCH" in arm64|x86_64) RUN="/usr/bin/arch -$ARCH" ;; esac
  printf '%s' "$ARCH" > "$PREFIX/arch"          # a downloaded Tracewright.app reads it to start the engine
fi

cat > "$BIN/tracewright" <<EOF
#!/bin/sh
exec $RUN "$VENV/bin/python" -m tracewright "\$@"
EOF
chmod +x "$BIN/tracewright"
echo "Command: $BIN/tracewright   (add $BIN to your PATH if it is not there)"

if [ "$(uname)" = "Darwin" ] && [ "$1" != "--engine-only" ]; then
  APP="$HOME/Applications/Tracewright.app"
  mkdir -p "$HOME/Applications"
  # A running Tracewright.app keeps its old window until it is quit: ask it to quit first.
  osascript -e 'if application id "app.tracewright" is running then tell application id "app.tracewright" to quit' >/dev/null 2>&1 || true
  if command -v swiftc >/dev/null 2>&1 && sh "$HERE/macos/build.sh" "$APP" "$VENV/bin/python" "$ARCH"; then
    echo "App:     $APP (its own window)"
  else
    # No Swift compiler (xcode-select --install adds it): a small launcher that opens the browser.
    echo "Tracewright.app opens Tracewright in your browser (install the Xcode command line tools for its own window)."
    rm -rf "$APP"
    mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
    VERSION="$(sed -n 's/^version *= *"\(.*\)"/\1/p' "$HERE/pyproject.toml" | head -1)"
    sed -e "s|@VERSION@|$VERSION|g" -e "s|@PYTHON@|$VENV/bin/python|g" -e "s|@ARCH@|$ARCH|g" "$HERE/macos/Info.plist" > "$APP/Contents/Info.plist"
    cat > "$APP/Contents/MacOS/Tracewright" <<EOF
#!/bin/sh
# Start Tracewright (or reach the one already running) and open it in the browser.
nohup $RUN "$VENV/bin/python" -m tracewright >> "\$HOME/Library/Logs/Tracewright.log" 2>&1 &
EOF
    chmod +x "$APP/Contents/MacOS/Tracewright"
  fi
fi

echo
$RUN "$VENV/bin/python" -m tracewright doctor || true
echo
echo "Start it: open Tracewright from ~/Applications, or run: tracewright"
