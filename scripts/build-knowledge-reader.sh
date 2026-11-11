#!/bin/sh
set -eu
ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
OUTPUT="$ROOT/desktop/src-tauri/binaries/olcr-knowledge-reader"
mkdir -p "$(dirname "$OUTPUT")"
if [ "$(uname -s)" = Darwin ]; then
  xcrun swiftc -O -module-cache-path "${TMPDIR:-/private/tmp}/olcr-swift-module-cache" -target "$(uname -m)-apple-macosx13.0" \
    -framework Foundation -framework PDFKit -framework Vision -framework AppKit -framework ImageIO \
    "$ROOT/desktop/src-tauri/knowledge-reader.swift" -o "$OUTPUT"
else
  printf '#!/bin/sh\nprintf '\''%%s\\n'\'' '\''{"error":"AGENT_KNOWLEDGE_READER_UNAVAILABLE"}'\''\n' > "$OUTPUT"
fi
chmod 755 "$OUTPUT"
