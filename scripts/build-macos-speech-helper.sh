#!/bin/sh
set -eu
ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
OUTPUT="$ROOT/desktop/src-tauri/binaries/olcr-macos-speech"
mkdir -p "$(dirname "$OUTPUT")"
if [ "$(uname -s)" = Darwin ]; then
  xcrun swiftc -O -module-cache-path "${TMPDIR:-/private/tmp}/olcr-swift-module-cache" -target "$(uname -m)-apple-macosx13.0" \
    -framework Foundation -framework Speech \
    "$ROOT/desktop/src-tauri/macos-speech-helper.swift" -o "$OUTPUT"
else
  cat > "$OUTPUT" <<'STUB'
#!/bin/sh
printf '%s\n' '{"type":"error","error":"MACOS_SPEECH_UNAVAILABLE"}'
STUB
fi
chmod 755 "$OUTPUT"
sh "$ROOT/scripts/build-knowledge-reader.sh"
sh "$ROOT/scripts/build-macos-calendar-helper.sh"
sh "$ROOT/scripts/build-macos-mail-helper.sh"
