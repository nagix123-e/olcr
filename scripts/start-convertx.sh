#!/bin/sh
# Optional developer-managed Docker service. The normal OLCR Settings switch
# provisions its own runtime and never invokes this script.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
if ! command -v docker >/dev/null 2>&1; then
  echo 'This optional developer service requires Docker. For normal use, turn Convert ON in OLCR Settings; no Docker installation is needed.' >&2
  exit 1
fi
docker compose -p olcr-convertx -f "$ROOT/packaging/convertx/compose.yaml" up -d --wait
echo 'ConvertX: http://127.0.0.1:3047 — OLCR can now convert attached files.'
