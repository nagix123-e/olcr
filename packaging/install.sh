#!/bin/sh
set -eu

VERSION="0.1.0"
ARCHIVE_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
APP_HOME="${OLCR_APP_SUPPORT:-$HOME/Library/Application Support/OLCR}"
RUNTIME_ROOT="$APP_HOME/runtime"
TARGET="$RUNTIME_ROOT/$VERSION"
BIN_DIR="${OLCR_BIN_DIR:-$HOME/.local/bin}"

case "$ARCHIVE_ROOT" in *"/olcr-v${VERSION}-macos-arm64") ;; *) echo "OLCR installer must run from the extracted release directory." >&2; exit 1;; esac
mkdir -p "$RUNTIME_ROOT" "$BIN_DIR"
if [ ! -d "$TARGET" ]; then
  mkdir -p "$TARGET"
  cp -R "$ARCHIVE_ROOT/app" "$ARCHIVE_ROOT/frontend" "$ARCHIVE_ROOT/models" "$ARCHIVE_ROOT/runtime" "$ARCHIVE_ROOT/manifest" "$ARCHIVE_ROOT/licenses" "$TARGET/"
  cp "$ARCHIVE_ROOT/README.txt" "$TARGET/"
fi
ln -sfn "$TARGET" "$RUNTIME_ROOT/current"
cat > "$BIN_DIR/olcr" <<'EOF'
#!/bin/sh
set -eu
APP_HOME="${OLCR_APP_SUPPORT:-$HOME/Library/Application Support/OLCR}"
ROOT="$APP_HOME/runtime/current"
if [ ! -x "$ROOT/runtime/python/bin/python3" ]; then
  echo "OLCR runtime is not installed. Re-run install.sh from the release archive." >&2
  exit 1
fi
export PYTHONPATH="$ROOT/app/backend:$ROOT/runtime/python/lib/python3.10/site-packages${PYTHONPATH:+:$PYTHONPATH}"
export OLCR_RERANKER_MODEL="$ROOT/models/qwen3-reranker-0.6b"
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
exec "$ROOT/runtime/python/bin/python3" -m olcr_cli.main "$@"
EOF
chmod 755 "$BIN_DIR/olcr"
echo "OLCR $VERSION installed at: $TARGET"
echo "Command installed at: $BIN_DIR/olcr"
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) echo "Add OLCR to this shell with: export PATH=\"$BIN_DIR:\$PATH\"";; esac
echo "Ollama is external. Run '$BIN_DIR/olcr status' for prerequisite diagnostics."
