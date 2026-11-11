#!/bin/sh
set -eu
ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
OUTPUT="$ROOT/desktop/src-tauri/binaries/olcr-macos-mail"
mkdir -p "$(dirname "$OUTPUT")"
if [ "$(uname -s)" = Darwin ]; then
  xcrun swiftc -O -module-cache-path "${TMPDIR:-/private/tmp}/olcr-swift-module-cache" -target "$(uname -m)-apple-macosx13.0" \
    -framework Foundation -framework AppKit -framework Carbon \
    -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist -Xlinker "$ROOT/desktop/src-tauri/mail-helper-Info.plist" \
    "$ROOT/desktop/src-tauri/macos-mail-helper.swift" -o "$OUTPUT"
  codesign --force --sign "OLCR Local Debug Code Signing" --entitlements "$ROOT/desktop/src-tauri/mail-helper-Entitlements.plist" "$OUTPUT"
else
  printf '#!/bin/sh\nprintf '\''%%s\\n'\'' '\''{"error":"APPLE_MAIL_UNAVAILABLE"}'\''\n' > "$OUTPUT"
fi
chmod 755 "$OUTPUT"
