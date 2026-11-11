#!/bin/sh
set -eu
ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
OUTPUT="$ROOT/desktop/src-tauri/binaries/olcr-macos-calendar"
mkdir -p "$(dirname "$OUTPUT")"
if [ "$(uname -s)" = Darwin ]; then
  xcrun swiftc -O -module-cache-path "${TMPDIR:-/private/tmp}/olcr-swift-module-cache" -target "$(uname -m)-apple-macosx13.0" \
    -framework Foundation -framework EventKit -framework CryptoKit -framework AppKit \
    -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist -Xlinker "$ROOT/desktop/src-tauri/calendar-helper-Info.plist" \
    "$ROOT/desktop/src-tauri/macos-calendar-helper.swift" -o "$OUTPUT"
  codesign --force --sign "OLCR Local Debug Code Signing" --entitlements "$ROOT/desktop/src-tauri/Entitlements.plist" "$OUTPUT"
else
  printf '#!/bin/sh\nprintf '\''%%s\\n'\'' '\''{"error":"MACOS_CALENDAR_UNAVAILABLE"}'\''\n' > "$OUTPUT"
fi
chmod 755 "$OUTPUT"
