#!/bin/bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: $0 <bundle-id> <output-app-path>" >&2
  exit 2
fi

BUNDLE_ID="$1"
APP_PATH="$2"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONTENTS="$APP_PATH/Contents"
MACOS="$CONTENTS/MacOS"

if [[ ! "$BUNDLE_ID" =~ ^local\.olcr\.[A-Za-z0-9.-]+$ ]]; then
  echo "fixture bundle ID must begin with local.olcr." >&2
  exit 2
fi

mkdir -p "$MACOS"
SDK_PATH="$(xcrun --sdk macosx --show-sdk-path)"
"$(xcrun --find swiftc)" -sdk "$SDK_PATH" -target arm64-apple-macosx13.0 \
  "$ROOT/tests/native_tracking_fixture/main.swift" -framework AppKit -o "$MACOS/OLCRTrackingFixture"
cat > "$CONTENTS/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleExecutable</key><string>OLCRTrackingFixture</string>
  <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
  <key>CFBundleName</key><string>OLCR OpenCV Tracking Fixture</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
</dict></plist>
PLIST
/usr/bin/codesign --force --sign - "$APP_PATH"
echo "FIXTURE_APP_PATH=$APP_PATH"
echo "FIXTURE_BUNDLE_ID=$BUNDLE_ID"
