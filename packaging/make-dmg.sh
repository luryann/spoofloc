#!/bin/bash
# Build Spoofloc.dmg for distribution
# Run from the project root: bash packaging/make-dmg.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
BUILD_DIR="$PROJECT_DIR/dist/dmg-staging"
APP_NAME="Spoofloc"
DMG_OUT="$PROJECT_DIR/dist/Spoofloc.dmg"

echo "==> Cleaning build directory..."
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"

# ---------------------------------------------------------------------------
# 1. Copy project source (friend installs from this)
# ---------------------------------------------------------------------------
echo "==> Copying project source..."
mkdir -p "$BUILD_DIR/spoofloc-src"
cp -r "$PROJECT_DIR/spoofloc" "$BUILD_DIR/spoofloc-src/"
cp "$PROJECT_DIR/pyproject.toml" "$BUILD_DIR/spoofloc-src/"
[ -f "$PROJECT_DIR/requirements.txt" ] && cp "$PROJECT_DIR/requirements.txt" "$BUILD_DIR/spoofloc-src/"
# Strip compiled bytecode — it's platform-specific and unnecessary
find "$BUILD_DIR/spoofloc-src" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

# ---------------------------------------------------------------------------
# 2. Create Spoofloc.app bundle
# ---------------------------------------------------------------------------
echo "==> Building Spoofloc.app..."
APP="$BUILD_DIR/$APP_NAME.app"
mkdir -p "$APP/Contents/MacOS"
mkdir -p "$APP/Contents/Resources"

# Info.plist
cat > "$APP/Contents/Info.plist" << 'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>Spoofloc</string>
    <key>CFBundleDisplayName</key>
    <string>Spoofloc</string>
    <key>CFBundleIdentifier</key>
    <string>com.spoofloc.app</string>
    <key>CFBundleVersion</key>
    <string>1.0</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleExecutable</key>
    <string>Spoofloc</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>LSMinimumSystemVersion</key>
    <string>13.0</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>LSUIElement</key>
    <false/>
</dict>
</plist>
PLIST

# Main launcher script
cat > "$APP/Contents/MacOS/Spoofloc" << 'LAUNCHER'
#!/bin/bash
# Spoofloc launcher — starts tunneld (with admin dialog) then opens the web UI

VENV="$HOME/Library/Application Support/spoofloc/venv"
PYTHON="$VENV/bin/python3"
SPOOFLOC="$VENV/bin/spoofloc"
TUNNELD_URL="http://127.0.0.1:49151"
MAP_URL="http://127.0.0.1:4780"
MAP_PID_FILE="/tmp/spoofloc-map.pid"

# ── Check installation ──────────────────────────────────────────────────────
if [ ! -x "$SPOOFLOC" ]; then
    osascript -e 'display alert "Spoofloc is not installed" message "Please open the Spoofloc DMG and run \"Install Spoofloc\" first." buttons {"OK"} default button "OK"'
    exit 0
fi

# ── Check if map server is already running ──────────────────────────────────
if curl -sf "$MAP_URL/" > /dev/null 2>&1; then
    open "$MAP_URL"
    osascript -e 'display notification "Opened in your browser." with title "Spoofloc"'
    exit 0
fi

# ── Start tunneld if not running ────────────────────────────────────────────
if ! curl -sf "$TUNNELD_URL/" > /dev/null 2>&1; then
    TUNNELD_CMD="nohup '$PYTHON' -m pymobiledevice3 remote tunneld >> /tmp/spoofloc-tunneld.log 2>&1 &"
    set +e
    osascript -e "do shell script \"$TUNNELD_CMD\" with administrator privileges with prompt \"Spoofloc needs permission to start the iOS device tunnel (this is required once per session).\""
    OSASCRIPT_EXIT=$?
    set -e

    if [ "$OSASCRIPT_EXIT" -ne 0 ]; then
        # User cancelled the password dialog
        exit 0
    fi

    # Wait up to 25 seconds for tunneld to respond
    echo "Waiting for device tunnel..."
    WAITED=0
    while [ "$WAITED" -lt 25 ]; do
        sleep 1
        WAITED=$((WAITED + 1))
        if curl -sf "$TUNNELD_URL/" > /dev/null 2>&1; then
            break
        fi
        if [ "$WAITED" -eq 25 ]; then
            osascript -e 'display alert "Tunnel did not start" message "The device tunnel did not respond after 25 seconds.\n\nMake sure your iPhone is:\n• Unlocked\n• On the same WiFi network as this Mac\n• Set up with \"Install Spoofloc\" (run it again if unsure)" buttons {"OK"} default button "OK"'
            exit 0
        fi
    done
fi

# ── Start the map server ────────────────────────────────────────────────────
nohup "$SPOOFLOC" map --no-browser >> /tmp/spoofloc-map.log 2>&1 &
echo $! > "$MAP_PID_FILE"
disown

# Wait for server to be ready (up to 10 seconds), then open browser
WAITED=0
while [ "$WAITED" -lt 10 ]; do
    sleep 1
    WAITED=$((WAITED + 1))
    if curl -sf "$MAP_URL/" > /dev/null 2>&1; then
        open "$MAP_URL"
        osascript -e 'display notification "Spoofloc is running. Use your browser to control it." with title "Spoofloc"'
        exit 0
    fi
done

# If server never came up, show error
osascript -e 'display alert "Spoofloc did not start" message "The map server did not start within 10 seconds. Check /tmp/spoofloc-map.log for details." buttons {"OK"} default button "OK"'
exit 0
LAUNCHER

chmod +x "$APP/Contents/MacOS/Spoofloc"

# ---------------------------------------------------------------------------
# 3. Create Install Spoofloc.command
# ---------------------------------------------------------------------------
echo "==> Creating install script..."
cat > "$BUILD_DIR/Install Spoofloc.command" << 'INSTALL'
#!/bin/bash
# Spoofloc one-time installer
# Double-click this file to run it in Terminal.

set -euo pipefail

APP_SUPPORT="$HOME/Library/Application Support/spoofloc"
VENV="$APP_SUPPORT/venv"

# Find the directory this script lives in (inside the DMG)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="$SCRIPT_DIR/spoofloc-src"

# ── Banner ──────────────────────────────────────────────────────────────────
clear
echo "╔══════════════════════════════════════════╗"
echo "║         Spoofloc Installer               ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── Check Python 3.11+ ──────────────────────────────────────────────────────
echo "Checking Python version..."
if ! command -v python3 &>/dev/null; then
    echo ""
    echo "ERROR: Python 3 not found."
    echo "Please install Python from https://www.python.org/downloads/"
    echo "Then re-run this installer."
    echo ""
    read -n1 -r -p "Press any key to exit..."
    exit 1
fi

PY_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$(python3 -c "import sys; print(sys.version_info.major)")
PY_MINOR=$(python3 -c "import sys; print(sys.version_info.minor)")

if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 11 ]; }; then
    echo ""
    echo "ERROR: Python $PY_VERSION found, but Python 3.11 or newer is required."
    echo "Please install Python 3.11+ from https://www.python.org/downloads/"
    echo "Then re-run this installer."
    echo ""
    read -n1 -r -p "Press any key to exit..."
    exit 1
fi

echo "  ✓ Python $PY_VERSION"

# ── Create virtual environment ───────────────────────────────────────────────
echo ""
echo "Setting up Spoofloc environment..."
mkdir -p "$APP_SUPPORT"

if [ -d "$VENV" ]; then
    echo "  Existing environment found — updating..."
    "$VENV/bin/pip" install --quiet --upgrade pip
else
    echo "  Creating virtual environment..."
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install --quiet --upgrade pip
fi

# ── Install spoofloc ─────────────────────────────────────────────────────────
echo "  Installing Spoofloc and dependencies (this may take a minute)..."
"$VENV/bin/pip" install --quiet "$SRC_DIR"
echo "  ✓ Spoofloc installed"

# ── Copy app to Applications ─────────────────────────────────────────────────
echo ""
echo "Installing Spoofloc.app to /Applications..."
APP_SRC="$SCRIPT_DIR/Spoofloc.app"
APP_DEST="/Applications/Spoofloc.app"

if [ -d "$APP_SRC" ]; then
    if [ -d "$APP_DEST" ]; then
        rm -rf "$APP_DEST"
    fi
    cp -r "$APP_SRC" "$APP_DEST"
    echo "  ✓ Spoofloc.app installed to /Applications"
else
    echo "  (Spoofloc.app not found in DMG — copy it manually to Applications)"
fi

# ── iPhone setup ─────────────────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  iPhone Setup"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "You need to do this once to pair your iPhone."
echo ""
echo "  1. Connect your iPhone to this Mac with a USB cable."
echo "  2. On your iPhone: Settings → Privacy & Security → Developer Mode → Enable."
echo "     (Your iPhone will restart.)"
echo "  3. After restart, unlock your iPhone and trust this Mac if prompted."
echo ""
read -r -p "Press Enter when ready (iPhone connected and Developer Mode on)..."

echo ""
echo "Running iPhone pairing..."
if ! "$VENV/bin/spoofloc" setup; then
    echo ""
    echo "  ⚠️  Setup didn't complete fully. This usually means the iPhone"
    echo "     wasn't detected. You can try again later by running:"
    echo "     ~/Library/Application\ Support/spoofloc/venv/bin/spoofloc setup"
fi

# ── Done ─────────────────────────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Installation complete!"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "To use Spoofloc:"
echo "  1. Open Spoofloc.app from your Applications folder."
echo "  2. Enter your Mac password when prompted (needed to start the device tunnel)."
echo "  3. Your browser will open with the Spoofloc map UI."
echo ""
echo "You can unplug the USB cable after setup. Spoofloc works over WiFi."
echo "(Make sure your iPhone and Mac are on the same WiFi network.)"
echo ""
read -n1 -r -p "Press any key to close..."
INSTALL

chmod +x "$BUILD_DIR/Install Spoofloc.command"

# ---------------------------------------------------------------------------
# 4. Create a simple README as a .command so it opens in Terminal
# ---------------------------------------------------------------------------
cat > "$BUILD_DIR/README.txt" << 'README'
SPOOFLOC — iOS Location Spoofing
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

FIRST TIME SETUP
  1. Double-click "Install Spoofloc" — this sets everything up.
     (It opens a Terminal window and walks you through it.)
  2. Follow the iPhone pairing steps when prompted.

DAILY USE
  • Open Spoofloc from your Applications folder.
  • Enter your Mac password when the dialog appears.
  • Your browser opens with the map UI — done!

FIRST LAUNCH WARNING
  The first time you open Spoofloc.app, macOS may warn you it's from an
  "unidentified developer." This is normal for apps not from the App Store.
  To open it: right-click (or Control-click) Spoofloc → Open → Open.
  You only need to do this once.

REQUIREMENTS
  • macOS 13 (Ventura) or newer
  • Python 3.11 or newer  (https://www.python.org/downloads/)
  • iPhone with Developer Mode enabled
  • Mac and iPhone on the same WiFi network
README

# ---------------------------------------------------------------------------
# 5. Build the zip (works on any platform)
# ---------------------------------------------------------------------------
echo "==> Building zip..."
ZIP_OUT="$PROJECT_DIR/dist/Spoofloc.zip"
rm -f "$ZIP_OUT"
cd "$BUILD_DIR"
zip -r "$ZIP_OUT" . --exclude "*.DS_Store"
cd "$PROJECT_DIR"

# ---------------------------------------------------------------------------
# 6. Build the DMG (macOS only)
# ---------------------------------------------------------------------------
if [[ "$(uname)" == "Darwin" ]]; then
    echo "==> Building DMG..."
    rm -f "$DMG_OUT"
    hdiutil create \
        -volname "Spoofloc" \
        -srcfolder "$BUILD_DIR" \
        -ov \
        -format UDZO \
        -fs HFS+ \
        "$DMG_OUT"
    echo ""
    echo "✓ Done! Artifacts created:"
    echo "  DMG: $DMG_OUT"
    echo "  Zip: $ZIP_OUT"
else
    echo ""
    echo "✓ Done! Artifact created:"
    echo "  Zip: $ZIP_OUT"
fi

echo ""
echo "Send Spoofloc.zip (or .dmg) to your friend. They should:"
echo "  1. Extract the zip (or open the DMG)"
echo "  2. Run 'Install Spoofloc' first"
echo "  3. Then use Spoofloc.app from Applications"
