#!/bin/zsh
set -euo pipefail

SCRIPT_PATH="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
ROOT="$(cd "$(dirname "$SCRIPT_PATH")/.." && pwd)"
LABEL="${OLCR_DEV_LAUNCHD_LABEL:-local.olcr.dev-independent}"
UID_VALUE="$(id -u)"
DOMAIN="gui/$UID_VALUE"
APP="$ROOT/desktop/src-tauri/target/debug/bundle/macos/OLCR.app"
BIN="$APP/Contents/MacOS/olcr-desktop"
OUT_LOG="/tmp/olcr-dev-independent.stdout.log"
ERR_LOG="/tmp/olcr-dev-independent.stderr.log"
SIGNING_IDENTITY="${OLCR_CUA_SIGNING_IDENTITY:-OLCR Local Debug Code Signing}"
PY="$ROOT/.venv312/bin/python"
SOURCE_STAMP="$ROOT/desktop/src-tauri/target/.olcr-dev-independent-source.sha256"

# The health build ID identifies a launch, not its code. Compare contents of
# runtime sources and build inputs, including uncommitted/new/deleted files.
# Keep generated output and runtime bytecode out of this identity.
source_fingerprint() {
  "$PY" - "$ROOT" <<'PY'
from pathlib import Path
import hashlib
import sys

root = Path(sys.argv[1])
paths = set()
for relative in (
    "backend/olcr_api", "backend/olcr_cli", "frontend/src", "frontend/public",
    "desktop/src-tauri/src", "desktop/src-tauri/capabilities", "desktop/src-tauri/icons",
):
    folder = root / relative
    if folder.is_dir():
        paths.update(path for path in folder.rglob("*") if path.is_file()
                     and "__pycache__" not in path.parts
                     and path.suffix not in {".pyc", ".pyo"} and path.name != ".DS_Store")
for pattern in (
    "frontend/*.json", "frontend/*.html", "frontend/vite.config.*",
    "desktop/src-tauri/Cargo.*", "desktop/src-tauri/*.rs", "desktop/src-tauri/*.json",
    "desktop/src-tauri/*.plist", "desktop/src-tauri/*.swift",
    "backend/requirements*.txt", "backend/*.lock", "backend/pyproject.toml", "pyproject.toml",
    "scripts/build-*.sh", "scripts/start-dev-independent.sh", "models/model_manifest.json",
):
    paths.update(path for path in root.glob(pattern) if path.is_file())
digest = hashlib.sha256(b"olcr-dev-source-v1\0")
for path in sorted(paths):
    name = path.relative_to(root).as_posix().encode()
    content = path.read_bytes()
    digest.update(len(name).to_bytes(8, "big"))
    digest.update(name)
    digest.update(len(content).to_bytes(8, "big"))
    digest.update(content)
print(digest.hexdigest())
PY
}

if [[ ! -x "$PY" ]]; then
  print -u2 "OLCR_PYTHON_RUNTIME_MISSING: $PY"
  exit 1
fi
SOURCE_HASH="$(source_fingerprint)"

# A sandbox may hide Keychain identities and deny ps. Do not interpret that
# environment as a deleted certificate or proceed to an expensive failed build.
if ! PROCESS_LIST="$(ps -axo command=)"; then
  print -u2 "OLCR_LAUNCH_ENVIRONMENT_RESTRICTED: rerun this launcher outside the sandbox with user-session access. Do not rotate signing identities."
  exit 1
fi
if ! /usr/bin/security find-identity -v -p codesigning | rg -Fq "\"$SIGNING_IDENTITY\""; then
  print -u2 "OLCR_SIGNING_IDENTITY_NOT_VISIBLE: verify Keychain access outside the sandbox before diagnosing a missing identity. Required: $SIGNING_IDENTITY"
  exit 1
fi

# `tauri dev` is a watcher: closing its window causes it to create another
# window. Build a bundle and let Launch Services wait for that app instance
# instead, so a normal Quit ends this job without reopening the app.
if launchctl list | awk -v label="$LABEL" '$3 == label { found = 1 } END { exit !found }'; then
  APP_PID="$(pgrep -f "$BIN$" | head -1 || true)"
  if [[ -n "$APP_PID" ]]; then
    if [[ ! -f "$SOURCE_STAMP" || "$(cat "$SOURCE_STAMP")" != "$SOURCE_HASH" ]]; then
      print -u2 "OLCR_SOURCE_STALE: the running OLCR source is different or cannot be verified (PID $APP_PID)."
      print -u2 "Quit OLCR normally, then rerun this script to build and launch the current source."
      exit 1
    fi
    print "OLCR_SOURCE_MATCH=true SOURCE_SHA256=$SOURCE_HASH"
    print "OLCR is already running under launchd: $LABEL (PID $APP_PID)"
    print "Logs: $OUT_LOG and $ERR_LOG"
    exit 0
  fi
  # A closed app leaves the non-KeepAlive LaunchAgent loaded but idle. Remove
  # that completed registration so a later explicit launch can start it again.
  launchctl bootout "$DOMAIN/$LABEL"
fi

if print -r -- "$PROCESS_LIST" | rg -q '[o]lcr-desktop|[t]auri dev'; then
  print -u2 "An OLCR desktop/dev process is already running; refusing to start a duplicate."
  exit 1
fi

BUILD_ID="olcr-dev-independent-$(date +%Y%m%d-%H%M%S)"
PLIST="$(mktemp "/private/tmp/${LABEL//./-}.XXXXXX.plist")"
cleanup_plist() { rm -f "$PLIST"; }
trap cleanup_plist EXIT

cd "$ROOT"
source "$HOME/.cargo/env"
cd "$ROOT/desktop"
env -u OLCR_SKIP_KEYCHAIN_RESTORE \
  OLCR_BACKEND_DIR="$ROOT/backend" \
  OLCR_PYTHON="$PY" \
  OLCR_API_REPAIR_BUILD_ID="olcr-dev-independent-build" \
  OLCR_BACKEND_STARTUP_TIMEOUT_SECONDS=180 \
  PYTHONUNBUFFERED=1 RUST_LOG=info \
  "$ROOT/frontend/node_modules/.bin/tauri" build --debug --bundles app

test -x "$BIN"
BUNDLE_ID="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$APP/Contents/Info.plist")"
[[ "$BUNDLE_ID" == "local.olcr.desktop" ]]
/usr/bin/codesign --force --deep --sign "$SIGNING_IDENTITY" --timestamp=none --entitlements "$ROOT/desktop/src-tauri/Entitlements.plist" --identifier "$BUNDLE_ID" "$APP"
/usr/bin/codesign --verify --deep --strict "$APP"
if [[ "$(source_fingerprint)" != "$SOURCE_HASH" ]]; then
  print -u2 "OLCR_SOURCE_CHANGED_DURING_BUILD: no app was launched; rerun this script after source edits finish."
  exit 1
fi

# Run Launch Services as a session-owned, one-shot job. KeepAlive=false is
# essential: ChatGPT/Codex may restart without affecting OLCR, but quitting
# OLCR must not restart the Tauri watcher or relaunch the bundle.
"$PY" - "$PLIST" "$LABEL" "$ROOT" "$APP" "$PY" "$BUILD_ID" "$OUT_LOG" "$ERR_LOG" <<'PY'
import plistlib
import sys

path, label, root, app, python, build_id, stdout, stderr = sys.argv[1:]
plist = {
    "Label": label,
    "RunAtLoad": True,
    "KeepAlive": False,
    "StandardOutPath": stdout,
    "StandardErrorPath": stderr,
    "ProgramArguments": [
        "/usr/bin/open", "-n", "-W", "--stdout", stdout, "--stderr", stderr,
        "--env", f"OLCR_BACKEND_DIR={root}/backend",
        "--env", f"OLCR_PYTHON={python}",
        "--env", f"OLCR_LAYA_CACHE={root}/models/laya",
        "--env", "OLCR_LAYA_CHECKPOINTS_READY=true",
        "--env", f"OLCR_MODEL_MANIFEST={root}/models/model_manifest.json",
        "--env", f"OLCR_API_REPAIR_BUILD_ID={build_id}",
        "--env", "OLCR_BACKEND_STARTUP_TIMEOUT_SECONDS=180",
        "--env", "OLCR_SKIP_KEYCHAIN_RESTORE=false",
        "--env", "OLCR_KEYCHAIN_BRIDGE_PATH=" + __import__("os").path.expanduser(
            "~/Library/Application Support/OLCR/KeychainBridge/olcr-keychain-bridge"),
        "--env", f"OLCR_ALLOWED_ROOTS={root}",
        "--env", "PYTHONUNBUFFERED=1", "--env", "RUST_LOG=info",
        "--env", "DYLD_LIBRARY_PATH=", "--env", "DYLD_FALLBACK_LIBRARY_PATH=",
        app,
    ],
}
with open(path, "wb") as handle:
    plistlib.dump(plist, handle)
PY

launchctl bootstrap "$DOMAIN" "$PLIST"
# Attest only a successfully built, signed, unchanged source launch request.
# A failed build/bootstrap must never bless a later pre-existing process.
print -r -- "$SOURCE_HASH" > "$SOURCE_STAMP"
cleanup_plist
trap - EXIT
print "OLCR_LAUNCH_REQUESTED=true SOURCE_SHA256=$SOURCE_HASH"
print "Launch requested for current OLCR source under one-shot launchd job: $LABEL"
print "Backend readiness has not been verified; confirm READY in OLCR."
print "Build ID: $BUILD_ID"
print "App: $APP"
print "Logs: $OUT_LOG and $ERR_LOG"
print "Quit OLCR normally; it will stay closed. Relaunch with this script."
print "Remove the launchd registration with: launchctl bootout $DOMAIN/$LABEL"
