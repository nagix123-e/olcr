#!/bin/sh
set -eu

VERSION="0.1.8"
ARCHIVE_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
APP_HOME="${OLCR_APP_SUPPORT:-$HOME/Library/Application Support/OLCR}"
RUNTIME_ROOT="$APP_HOME/runtime"
TARGET="$RUNTIME_ROOT/$VERSION"
BIN_DIR="${OLCR_BIN_DIR:-$HOME/.local/bin}"

quarantine_warning_shown=0
warn_quarantine_removal() {
  if [ "$quarantine_warning_shown" -eq 0 ]; then
    cat >&2 <<'EOF'
OLCR includes an unsigned, not-notarized bundled runtime.
macOS may quarantine it after download.

This installer will remove com.apple.quarantine only from OLCR's installed
runtime and launcher so OLCR can execute. Running install.sh constitutes
consent to this documented installation step.
EOF
    quarantine_warning_shown=1
  fi
}

has_quarantine() {
  xattr -r "$1" 2>/dev/null | grep -q 'com.apple.quarantine'
}

remove_runtime_quarantine() {
  runtime_path="$TARGET/runtime"
  if has_quarantine "$runtime_path"; then
    warn_quarantine_removal
    if ! xattr -dr com.apple.quarantine "$runtime_path"; then
      echo "Failed to remove macOS quarantine from OLCR runtime: $runtime_path" >&2
      echo "macOS may block OLCR from launching." >&2
      exit 1
    fi
    if has_quarantine "$runtime_path"; then
      echo "macOS quarantine remains on OLCR runtime: $runtime_path" >&2
      echo "macOS may block OLCR from launching." >&2
      exit 1
    fi
  fi
}

remove_launcher_quarantine() {
  if xattr -p com.apple.quarantine "$BIN_DIR/olcr" >/dev/null 2>&1; then
    warn_quarantine_removal
    if ! xattr -d com.apple.quarantine "$BIN_DIR/olcr"; then
      echo "Failed to remove macOS quarantine from OLCR launcher: $BIN_DIR/olcr" >&2
      echo "macOS may block OLCR from launching." >&2
      exit 1
    fi
    if xattr -p com.apple.quarantine "$BIN_DIR/olcr" >/dev/null 2>&1; then
      echo "macOS quarantine remains on OLCR launcher: $BIN_DIR/olcr" >&2
      echo "macOS may block OLCR from launching." >&2
      exit 1
    fi
  fi
}

case "$ARCHIVE_ROOT" in *"/olcr-v${VERSION}-macos-arm64") ;; *) echo "OLCR installer must run from the extracted release directory." >&2; exit 1;; esac
mkdir -p "$RUNTIME_ROOT" "$BIN_DIR"
if [ ! -d "$TARGET" ]; then
  mkdir -p "$TARGET"
  cp -R "$ARCHIVE_ROOT/app" "$ARCHIVE_ROOT/frontend" "$ARCHIVE_ROOT/models" "$ARCHIVE_ROOT/runtime" "$ARCHIVE_ROOT/manifest" "$ARCHIVE_ROOT/licenses" "$TARGET/"
  cp "$ARCHIVE_ROOT/README.txt" "$TARGET/"
fi
remove_runtime_quarantine
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
remove_launcher_quarantine
echo "OLCR $VERSION installed at: $TARGET"
echo "Command installed at: $BIN_DIR/olcr"
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) echo "Add OLCR to this shell with: export PATH=\"$BIN_DIR:\$PATH\"";; esac
echo "Ollama is external. Run '$BIN_DIR/olcr status' for prerequisite diagnostics."
