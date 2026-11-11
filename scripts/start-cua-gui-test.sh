#!/usr/bin/env bash
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
PY="${OLCR_PYTHON:-$REPO/.venv312/bin/python}"
for required_var in OLCR_CUA_TARGET_DIR OLCR_CUA_APP_SUPPORT OLCR_CUA_LOG OLCR_API_REPAIR_BUILD_ID; do
  if [[ -z "${!required_var:-}" || "${!required_var}" == *RUN_ID* ]]; then
    echo "$required_var must be set to a fresh, run-specific value" >&2
    exit 2
  fi
done
for path_var in OLCR_CUA_TARGET_DIR OLCR_CUA_APP_SUPPORT OLCR_CUA_LOG; do
  if [[ "${!path_var}" != /* ]]; then
    echo "$path_var must be an absolute path" >&2
    exit 2
  fi
done
CARGO_TARGET_DIR="$OLCR_CUA_TARGET_DIR"
APP_SUPPORT="$OLCR_CUA_APP_SUPPORT"
LOG="$OLCR_CUA_LOG"
BUILD_ID="$OLCR_API_REPAIR_BUILD_ID"
UI_ONLY="${OLCR_CUA_UI_ONLY:-false}"
PROFILE_SEED="${OLCR_CUA_PROFILE_SEED:-}"
if [[ "$UI_ONLY" == "true" ]]; then
  export VITE_OLCR_UI_PREVIEW=models OLCR_UI_ONLY_BUILD=models
else
  unset VITE_OLCR_UI_PREVIEW OLCR_UI_ONLY_BUILD
fi
# Keep run-specific metadata out of the compiled executable. The unique
# per-run BUILD_ID is supplied to the signed app at Launch Services startup
# below; this removes one avoidable source of binary/signature variation.
CODESIGN_STABLE_BUILD_ID="olcr-cua-stable-screencapture-client-v1"
for unique_path in "$CARGO_TARGET_DIR" "$APP_SUPPORT" "$LOG"; do
  if [[ -e "$unique_path" ]]; then
    echo "Refusing to reuse existing E2E path: $unique_path" >&2
    exit 2
  fi
done
mkdir -p "$CARGO_TARGET_DIR"
CARGO_TARGET_DIR="$(cd "$CARGO_TARGET_DIR" && pwd -P)"
export CARGO_TARGET_DIR
mkdir -p "$APP_SUPPORT"
if [[ -n "$PROFILE_SEED" ]]; then
  if [[ "$PROFILE_SEED" != /* || ! -f "$PROFILE_SEED/olcr.db" ]]; then
    echo "OLCR_CUA_PROFILE_SEED must be an absolute path containing olcr.db" >&2
    exit 2
  fi
  "$PY" - "$PROFILE_SEED/olcr.db" "$APP_SUPPORT/olcr.db" <<'PY'
import sqlite3
import sys

source_path, destination_path = sys.argv[1:]
source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
destination = sqlite3.connect(destination_path)
try:
    source.backup(destination)
    result = destination.execute("PRAGMA integrity_check").fetchone()[0]
    if result != "ok":
        raise SystemExit(f"Seeded profile integrity check failed: {result}")
finally:
    destination.close()
    source.close()
print("ISOLATED_PROFILE_SEED_BACKUP=PASS")
PY
  if [[ -d "$PROFILE_SEED/artifacts" ]]; then
    mkdir -p "$APP_SUPPORT/artifacts"
    cp -R "$PROFILE_SEED/artifacts/." "$APP_SUPPORT/artifacts/"
  fi
fi
APP="$CARGO_TARGET_DIR/debug/bundle/macos/OLCR.app"
BIN="$APP/Contents/MacOS/olcr-desktop"
SKIP_KEYCHAIN_RESTORE="${OLCR_CUA_SKIP_KEYCHAIN_RESTORE:-false}"
BACKEND_STARTUP_TIMEOUT="${OLCR_CUA_BACKEND_STARTUP_TIMEOUT_SECONDS:-180}"
SIGNING_IDENTITY="${OLCR_CUA_SIGNING_IDENTITY:-OLCR Local Debug Code Signing}"
if [[ "$SKIP_KEYCHAIN_RESTORE" != "false" ]]; then
  echo "Refusing GUI E2E with Keychain restoration disabled; set OLCR_CUA_SKIP_KEYCHAIN_RESTORE=false" >&2
  exit 2
fi
if ! [[ "$BACKEND_STARTUP_TIMEOUT" =~ ^[0-9]+$ ]] || (( BACKEND_STARTUP_TIMEOUT < 5 || BACKEND_STARTUP_TIMEOUT > 300 )); then
  echo "OLCR_CUA_BACKEND_STARTUP_TIMEOUT_SECONDS must be between 5 and 300" >&2
  exit 2
fi
READY_WAIT_ITERATIONS="$(((BACKEND_STARTUP_TIMEOUT + 30) * 2))"

cd "$REPO"
source "$HOME/.cargo/env"

echo "=== OLCR Computer Use bundled debug launch ==="
echo "REPO=$REPO"
echo "APP=$APP"
echo "BUILD_ID=$BUILD_ID"
echo "APP_SUPPORT=$APP_SUPPORT"
echo "LOG=$LOG"
echo "BACKEND_STARTUP_TIMEOUT_SECONDS=$BACKEND_STARTUP_TIMEOUT"
echo "SIGNING_IDENTITY=$SIGNING_IDENTITY"
echo "KEYCHAIN_RESTORE=ENABLED"

# Tauri owns the backend child, but a force-closed app can orphan that child
# under launchd.  An orphan may still hold the user database, so never clean
# one up automatically. Refuse to launch and let the operator diagnose it.
reject_orphaned_backends() {
  found=false
  while read -r pid; do
    [[ -z "$pid" ]] && continue
    command_line="$(ps -p "$pid" -o command= 2>/dev/null || true)"
    parent_pid="$(ps -p "$pid" -o ppid= 2>/dev/null | tr -d '[:space:]' || true)"
    case "$command_line" in
      *"-m uvicorn olcr_api.app:app --host 127.0.0.1 --port"*)
        cwd="$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p')"
        if [[ "$parent_pid" == "1" && "$cwd" == "$REPO/backend" ]]; then
          echo "ORPHANED_OLCR_BACKEND_PID=$pid CWD=$cwd; refusing to launch. Diagnose its port/profile and close only the confirmed test session from its app menu."
          found=true
        fi
        ;;
    esac
  done < <(pgrep -f 'uvicorn olcr_api\.app:app --host 127\.0\.0\.1 --port' || true)
  [[ "$found" == false ]]
}

# A force-closed app can also leave the MLX request worker detached from its
# backend. Match only this repository's exact worker entry point, but refuse to
# launch instead of terminating a process whose ownership/profile is unknown.
reject_orphaned_mlx_workers() {
  found=false
  while read -r pid; do
    [[ -z "$pid" ]] && continue
    command_line="$(ps -p "$pid" -o command= 2>/dev/null || true)"
    parent_pid="$(ps -p "$pid" -o ppid= 2>/dev/null | tr -d '[:space:]' || true)"
    case "$command_line" in
      *"$REPO/backend/olcr_api/mlx_worker.py --operation"*|*"$REPO/backend/olcr_api/audio_mlx_worker.py"*)
        cwd="$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p')"
        if [[ "$parent_pid" == "1" && ( "$cwd" == "$REPO/backend" || "$cwd" == "$REPO" ) ]]; then
          echo "ORPHANED_OLCR_MLX_WORKER_PID=$pid CWD=$cwd; refusing to launch. Diagnose its owning backend before continuing."
          found=true
        fi
        ;;
    esac
  done < <(pgrep -f 'backend/olcr_api/(mlx_worker\.py --operation|audio_mlx_worker\.py)' || true)
  [[ "$found" == false ]]
}

while read -r pid; do
  [[ -z "$pid" ]] && continue
  command_line="$(ps -p "$pid" -o command= 2>/dev/null || true)"
  if [[ "$command_line" == "$BIN" ]]; then
    echo "An OLCR GUI is already running from $BIN (PID $pid); refusing to terminate an active user session."
    exit 1
  fi
  case "$command_line" in
    */OLCR.app/Contents/MacOS/olcr-desktop)
      echo "Another OLCR GUI is already running from $command_line (PID $pid); refusing to launch beside a different app/profile."
      exit 1
      ;;
  esac
done < <(pgrep -f 'OLCR\.app/Contents/MacOS/olcr-desktop$' || true)
reject_orphaned_backends || exit 1
reject_orphaned_mlx_workers || exit 1

if [[ "${OLCR_CUA_OPENCV:-false}" == "true" ]]; then
  XCODE_DEV_DIR="$(xcode-select -p)"
  export DYLD_FALLBACK_LIBRARY_PATH="$XCODE_DEV_DIR/Toolchains/XcodeDefault.xctoolchain/usr/lib/swift-5.5/macosx:$XCODE_DEV_DIR/Toolchains/XcodeDefault.xctoolchain/usr/lib${DYLD_FALLBACK_LIBRARY_PATH:+:$DYLD_FALLBACK_LIBRARY_PATH}"
  export DYLD_LIBRARY_PATH="$XCODE_DEV_DIR/Toolchains/XcodeDefault.xctoolchain/usr/lib${DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}"
  export OPENCV_LINK_LIBS=opencv_video,opencv_features,opencv_imgproc,opencv_geometry,opencv_flann,opencv_core
  npm --prefix "$REPO/frontend" run build
  (cd "$REPO/desktop/src-tauri" && OLCR_API_REPAIR_BUILD_ID="$CODESIGN_STABLE_BUILD_ID" cargo build --features opencv-perception,custom-protocol)
  (cd "$REPO/desktop" && "$REPO/frontend/node_modules/.bin/tauri" bundle --debug --bundles app)
  python3 "$REPO/scripts/bundle_opencv_macos.py" "$APP"
else
  cd "$REPO/desktop"
  env -u OLCR_SKIP_KEYCHAIN_RESTORE \
  OLCR_BACKEND_DIR="$REPO/backend" \
  OLCR_PYTHON="$PY" \
  OLCR_API_REPAIR_BUILD_ID="$CODESIGN_STABLE_BUILD_ID" \
  OLCR_BACKEND_STARTUP_TIMEOUT_SECONDS="$BACKEND_STARTUP_TIMEOUT" \
  PYTHONUNBUFFERED=1 \
  RUST_LOG=info \
  "$REPO/frontend/node_modules/.bin/tauri" build --debug --bundles app
fi

cd "$REPO"
test -x "$BIN"
BUNDLE_ID="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$APP/Contents/Info.plist")"
[[ "$BUNDLE_ID" == "local.olcr.desktop" ]]
"/usr/bin/codesign" --force --deep --sign "$SIGNING_IDENTITY" --timestamp=none --entitlements "$REPO/desktop/src-tauri/Entitlements.plist" --identifier "$BUNDLE_ID" "$APP"
"/usr/bin/codesign" --verify --deep --strict "$APP"
"/usr/bin/codesign" -dv --verbose=2 "$APP" 2>&1 | rg 'Identifier=|Authority=|CDHash=|Signature=|TeamIdentifier='
# Launch Services must own the GUI process so macOS attributes TCC consent to
# the signed app bundle. A direct shell exec can inherit the terminal's identity.
# Pass isolation explicitly: Launch Services does not inherit this shell's env.
/usr/bin/open -n -W --stdout /dev/null --stderr "$LOG" \
  --env "OLCR_BACKEND_DIR=$REPO/backend" \
  --env "OLCR_PYTHON=$PY" \
  --env "OLCR_LAYA_CACHE=$REPO/models/laya" \
  --env "OLCR_LAYA_CHECKPOINTS_READY=true" \
  --env "OLCR_MODEL_MANIFEST=$REPO/models/model_manifest.json" \
  --env "OLCR_API_REPAIR_BUILD_ID=$BUILD_ID" \
  --env "OLCR_BACKEND_STARTUP_TIMEOUT_SECONDS=$BACKEND_STARTUP_TIMEOUT" \
  --env "OLCR_SKIP_KEYCHAIN_RESTORE=$SKIP_KEYCHAIN_RESTORE" \
  --env "OLCR_KEYCHAIN_BRIDGE_PATH=${OLCR_CUA_KEYCHAIN_BRIDGE_PATH:-${HOME:?}/Library/Application Support/OLCR/KeychainBridge/olcr-keychain-bridge}" \
  --env "OLCR_APP_SUPPORT=$APP_SUPPORT" \
  --env "OLCR_ALLOWED_ROOTS=$REPO" \
  --env "PYTHONUNBUFFERED=1" --env "RUST_LOG=info" \
  --env "DYLD_LIBRARY_PATH=" --env "DYLD_FALLBACK_LIBRARY_PATH=" "$APP" &
LAUNCH_SERVICES_WAITER_PID=$!
OLCR_PID=""
for _ in {1..120}; do
  APP_PIDS=()
  while read -r pid; do
    [[ -z "$pid" ]] && continue
    command_line="$(ps -ww -p "$pid" -o command= 2>/dev/null | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    [[ "$command_line" == "$BIN" ]] && APP_PIDS+=("$pid")
  done < <(pgrep -f 'OLCR\.app/Contents/MacOS/olcr-desktop$' || true)
  if (( ${#APP_PIDS[@]} == 1 )); then OLCR_PID="${APP_PIDS[0]}"; break; fi
  if (( ${#APP_PIDS[@]} > 1 )); then
    echo "EXACT_APP_PROCESS_AMBIGUOUS=true" >&2; exit 1
  fi
  sleep 0.25
done
if [[ -z "$OLCR_PID" ]]; then
  echo "LAUNCH_SERVICES_APP_PID_TIMEOUT=true LOG=$LOG" >&2
  exit 1
fi
trap 'kill "$OLCR_PID" 2>/dev/null || true' EXIT INT TERM
echo "OLCR_LAUNCH_METHOD=LAUNCH_SERVICES"

if [[ "$UI_ONLY" == "true" ]]; then
  for _ in {1..120}; do
    grep -q 'UI_ONLY_BUILD_READY=true BACKEND_DISABLED=true' "$LOG" && break
    kill -0 "$OLCR_PID" 2>/dev/null || exit 1
    sleep 0.25
  done
  grep -q 'UI_ONLY_BUILD_READY=true BACKEND_DISABLED=true' "$LOG"
  grep -Fq "UI_BUILD_ID=$BUILD_ID" "$LOG"
  APP_COMMAND="$(ps -ww -p "$OLCR_PID" -o command= | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
  [[ "$APP_COMMAND" == "$BIN" ]]
  if grep -q 'backend_spawn_started=true' "$LOG" || pgrep -P "$OLCR_PID" >/dev/null; then
    echo "UI_ONLY_UNEXPECTED_CHILD_PROCESS=true" >&2
    exit 1
  fi
  echo "READY_FOR_COMPUTER_USE=YES"
  echo "UI_ONLY_BUILD=YES BACKEND_DISABLED=YES"
  echo "OLCR_APP_PATH=$APP"
  echo "COMPUTER_USE_APP_SELECTOR=$APP"
  echo "OLCR_PID=$OLCR_PID"
  echo "BACKEND_PID=NONE BACKEND_PORT=NONE DB=NONE"
  echo "APP_SUPPORT=$APP_SUPPORT BUILD_ID=$BUILD_ID LOG=$LOG"
  wait "$LAUNCH_SERVICES_WAITER_PID"
  exit 0
fi

for ((attempt=0; attempt<READY_WAIT_ITERATIONS; attempt++)); do
  if grep -q 'backend_api_ready=true' "$LOG"; then break; fi
  if ! kill -0 "$OLCR_PID" 2>/dev/null; then
    cat "$LOG"
    exit 1
  fi
  sleep 0.5
done

if ! grep -q 'backend_api_ready=true' "$LOG"; then
  echo "BACKEND_API_READY_TIMEOUT=true BUILD_ID=$BUILD_ID LOG=$LOG" >&2
  cat "$LOG" >&2
  exit 1
fi
grep -Fq "API_REPAIR_BUILD_ID=$BUILD_ID" "$LOG"

# The port is emitted when the WebView invokes backend_config.  Wait for that
# handshake before probing the API; reading it immediately after backend
# startup races the frontend bootstrap and made the launcher abort even when
# the app itself was healthy.
for _ in {1..120}; do
  if grep -q 'FRONTEND_BACKEND_CONFIG_REQUESTED=true' "$LOG"; then break; fi
  if ! kill -0 "$OLCR_PID" 2>/dev/null; then
    cat "$LOG"
    exit 1
  fi
  sleep 0.25
done
if ! grep -q 'FRONTEND_BACKEND_CONFIG_REQUESTED=true' "$LOG"; then
  echo "FRONTEND_BACKEND_CONFIG_TIMEOUT=true BUILD_ID=$BUILD_ID LOG=$LOG" >&2
  cat "$LOG" >&2
  exit 1
fi

# Bind the identity check to the exact process started by this launcher. A
# global executable-name query can accidentally count/select a different
# OLCR.app bundle or miss which same-named window owns this backend.
APP_COMMAND="$(ps -ww -p "$OLCR_PID" -o command= 2>/dev/null | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
[[ "$APP_COMMAND" == "$BIN" ]]
COUNT=1

PORT="$(sed -n 's/.*BACKEND_PORT=\([0-9][0-9]*\).*/\1/p' "$LOG" | tail -1)"
test -n "$PORT"
curl --fail --silent "http://127.0.0.1:$PORT/api/health" | \
  "$PY" -c 'import json,sys; from pathlib import Path; health=json.load(sys.stdin); expected_build,expected_support=sys.argv[1:3]; actual_build=health.get("build_id"); actual_support=str(Path(health.get("app_support","")).resolve()); expected_support=str(Path(expected_support).resolve()); actual_db=Path(health.get("db_path","")).resolve(); print(f"HEALTH_BUILD_ID={actual_build} HEALTH_APP_SUPPORT_MATCH={actual_support == expected_support} HEALTH_DB_WITHIN_PROFILE={actual_db.parent == Path(expected_support)}"); sys.exit(0 if actual_build == expected_build and actual_support == expected_support and actual_db.parent == Path(expected_support) else 1)' "$BUILD_ID" "$APP_SUPPORT"
# The inventory check covers every Ollama deployment in the manifest, including
# optional fallbacks such as Qwen3-VL.  A missing optional Ollama model must not
# block computer-use E2E when the role registry itself is valid (for example,
# the GUI-grounding role is provided by the separately bundled MLX runtime).
# Keep inventory status visible for diagnosis, but gate readiness on the
# required role registry instead of requiring every fallback to be installed.
grep -q 'MODEL_REGISTRY_STATUS=PASS' "$LOG"
RUNTIME_BINDINGS_STATUS="$(sed -n 's/.*MODEL_RUNTIME_BINDINGS_STATUS=\([A-Z]*\).*/\1/p' "$LOG" | tail -1)"
echo "MODEL_RUNTIME_BINDINGS_STATUS=$RUNTIME_BINDINGS_STATUS (diagnostic; optional Ollama inventory does not gate GUI E2E)"
BACKEND_PIDS=()
while read -r pid; do
  [[ -z "$pid" ]] && continue
  BACKEND_PIDS+=("$pid")
done < <(lsof -nP -t -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | sort -u || true)
BACKEND_COUNT="${#BACKEND_PIDS[@]}"
[[ "$BACKEND_COUNT" == "1" ]]
BACKEND_PID="$(sed -n 's/.*backend_spawn_pid=\([0-9][0-9]*\).*/\1/p' "$LOG" | tail -1)"
[[ -n "$BACKEND_PID" && "${BACKEND_PIDS[0]}" == "$BACKEND_PID" ]]
BACKEND_PARENT="$(ps -p "$BACKEND_PID" -o ppid= 2>/dev/null | tr -d '[:space:]')"
[[ "$BACKEND_PARENT" == "$OLCR_PID" ]]

echo "READY_FOR_COMPUTER_USE=YES"
echo "OLCR_APP_PATH=$APP"
echo "BUNDLE_ID=$BUNDLE_ID"
echo "COMPUTER_USE_APP_SELECTOR=$APP"
echo "COMPUTER_USE_APP_SELECTION_RULE=EXACT_ABSOLUTE_PATH_ONLY"
echo "OLCR_PID=$OLCR_PID"
echo "OLCR_PROCESS_COUNT=$COUNT"
echo "BACKEND_PID=$BACKEND_PID"
echo "BACKEND_PORT=$PORT"
echo "BACKEND_PROCESS_COUNT=$BACKEND_COUNT"
echo "LOG=$LOG"

wait "$LAUNCH_SERVICES_WAITER_PID"
