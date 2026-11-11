#!/usr/bin/env bash
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${OLCR_KEYCHAIN_BRIDGE_INSTALL_PATH:-${HOME:?}/Library/Application Support/OLCR/KeychainBridge/olcr-keychain-bridge}"
DEST_DIR="$(dirname "$DEST")"
SIGNING_IDENTITY="${OLCR_CUA_SIGNING_IDENTITY:-OLCR Local Debug Code Signing}"
IDENTIFIER="local.olcr.keychain-bridge"
CERT_ROOT="969372f8911d3a2a85ea3339df864961a663f683"
REQUIREMENT="identifier \"$IDENTIFIER\" and certificate root = H\"$CERT_ROOT\""
STAGING_DIR="$(mktemp -d "${TMPDIR:-/private/tmp}/olcr-keychain-bridge-build.XXXXXX")"
STAGED_BINARY="$STAGING_DIR/olcr-keychain-bridge"
NEXT_DEST="$DEST_DIR/.olcr-keychain-bridge.next"
trap '"/bin/rm" -f "$STAGED_BINARY" "$NEXT_DEST"; "/bin/rmdir" "$STAGING_DIR" 2>/dev/null || true' EXIT

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "The OLCR Keychain bridge is supported only on macOS." >&2
  exit 2
fi
if [[ -e "$DEST" || -L "$DEST" ]]; then
  if [[ "${OLCR_KEYCHAIN_BRIDGE_REPLACE_EXISTING:-false}" != "true" ]]; then
    echo "Refusing to replace an existing stable Keychain bridge without OLCR_KEYCHAIN_BRIDGE_REPLACE_EXISTING=true: $DEST" >&2
    exit 2
  fi
  if ! /usr/bin/codesign --verify --strict --requirements="=identifier \"$IDENTIFIER\" and certificate root = H\"$CERT_ROOT\"" "$DEST"; then
    echo "Refusing to replace an unrecognized Keychain bridge: $DEST" >&2
    exit 2
  fi
fi
if ! /usr/bin/security find-identity -v -p codesigning 2>/dev/null | rg -Fq "\"$SIGNING_IDENTITY\""; then
  echo "Required signing identity is unavailable: $SIGNING_IDENTITY" >&2
  exit 2
fi

/usr/bin/swiftc -O -o "$STAGED_BINARY" "$REPO/desktop/keychain-bridge/main.swift"
/bin/chmod 700 "$STAGED_BINARY"
/usr/bin/codesign --force --sign "$SIGNING_IDENTITY" --timestamp=none --identifier "$IDENTIFIER" --requirements "=designated => $REQUIREMENT" "$STAGED_BINARY"
/usr/bin/codesign --verify --strict --requirements="=identifier \"$IDENTIFIER\" and certificate root = H\"$CERT_ROOT\"" "$STAGED_BINARY"

/bin/mkdir -p "$DEST_DIR"
/bin/chmod 700 "$DEST_DIR"
/usr/bin/install -m 700 "$STAGED_BINARY" "$NEXT_DEST"
/usr/bin/codesign --verify --strict --requirements="=identifier \"$IDENTIFIER\" and certificate root = H\"$CERT_ROOT\"" "$NEXT_DEST"
/bin/mv -f "$NEXT_DEST" "$DEST"
/usr/bin/codesign --verify --strict --requirements="=identifier \"$IDENTIFIER\" and certificate root = H\"$CERT_ROOT\"" "$DEST"

echo "KEYCHAIN_BRIDGE_INSTALLED=true path=$DEST"
/usr/bin/codesign -dv --verbose=2 "$DEST" 2>&1 | rg 'Identifier=|Authority=|CDHash=|TeamIdentifier='
