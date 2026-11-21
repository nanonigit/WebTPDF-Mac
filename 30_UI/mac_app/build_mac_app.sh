#!/usr/bin/env zsh
set -euo pipefail

SCRIPT_DIR=${0:a:h}
PRODUCT_ROOT=${SCRIPT_DIR:h:h}
BUILD_DIR="$SCRIPT_DIR/.build"
APP_DIR="$PRODUCT_ROOT/40_Ops/apps/WebTPDF Mac.app"
CONTENTS_DIR="$APP_DIR/Contents"
MACOS_DIR="$CONTENTS_DIR/MacOS"
RESOURCES_DIR="$CONTENTS_DIR/Resources"
EXECUTABLE="$MACOS_DIR/WebTPDFMac"
CODESIGN_IDENTITY=${CODESIGN_IDENTITY:-"-"}
GIT_COMMIT_COUNT=$(git -C "$PRODUCT_ROOT" rev-list --count HEAD 2>/dev/null || echo "1")
GIT_SHORT_SHA=$(git -C "$PRODUCT_ROOT" rev-parse --short HEAD 2>/dev/null || echo "dev")
APP_VERSION="0.1.${GIT_COMMIT_COUNT}"

mkdir -p "$MACOS_DIR" "$RESOURCES_DIR" "$BUILD_DIR"

cp "$SCRIPT_DIR/update_mac_app.sh" "$RESOURCES_DIR/update_mac_app.sh"
cp "$PRODUCT_ROOT/20_Backend/scripts/url_pdf_fetcher.py" "$RESOURCES_DIR/url_pdf_fetcher.py"
chmod +x "$RESOURCES_DIR/url_pdf_fetcher.py"
cp "$PRODUCT_ROOT/20_Backend/scripts/local_audiobook.py" "$RESOURCES_DIR/local_audiobook.py"
chmod +x "$RESOURCES_DIR/local_audiobook.py"
cp "$PRODUCT_ROOT/20_Backend/scripts/pdf_squeezer.py" "$RESOURCES_DIR/pdf_squeezer.py"
chmod +x "$RESOURCES_DIR/pdf_squeezer.py"
if [[ -f "$SCRIPT_DIR/Resources/AppIcon.icns" ]]; then
  cp "$SCRIPT_DIR/Resources/AppIcon.icns" "$RESOURCES_DIR/AppIcon.icns"
fi

swiftc \
  -O \
  -parse-as-library \
  -o "$EXECUTABLE" \
  "$SCRIPT_DIR/Sources/WebTPDF/App.swift" \
  "$SCRIPT_DIR/Sources/WebTPDF/PDFLibraryPreferences.swift" \
  -framework SwiftUI \
  -framework AVFoundation \
  -framework PDFKit \
  -framework AppKit \
  -framework UserNotifications

cat > "$CONTENTS_DIR/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDevelopmentRegion</key>
  <string>en</string>
  <key>CFBundleDisplayName</key>
  <string>WebTPDF Mac</string>
  <key>CFBundleExecutable</key>
  <string>WebTPDFMac</string>
  <key>CFBundleIconFile</key>
  <string>AppIcon</string>
  <key>CFBundleIdentifier</key>
  <string>com.local.webtpdf.mac</string>
  <key>CFBundleInfoDictionaryVersion</key>
  <string>6.0</string>
  <key>CFBundleName</key>
  <string>WebTPDF Mac</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleShortVersionString</key>
  <string>${APP_VERSION}</string>
  <key>CFBundleVersion</key>
  <string>${GIT_SHORT_SHA}</string>
  <key>LSMinimumSystemVersion</key>
  <string>13.0</string>
  <key>NSHighResolutionCapable</key>
  <true/>
  <key>LSApplicationCategoryType</key>
  <string>public.app-category.productivity</string>
</dict>
</plist>
PLIST

chmod +x "$EXECUTABLE"
plutil -lint "$CONTENTS_DIR/Info.plist" >/dev/null
if [[ "$CODESIGN_IDENTITY" == "-" ]]; then
  codesign --force --deep --sign - "$APP_DIR" >/dev/null
else
  codesign --force --deep --options runtime --timestamp --sign "$CODESIGN_IDENTITY" "$APP_DIR" >/dev/null
fi

echo "$APP_DIR"
