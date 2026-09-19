#!/usr/bin/env bash
# Generates a random bearer token for the producer's optional /mermelada
# trigger endpoint (producer/server.py, k8s/producer-pod.yaml) and saves
# it locally (git-ignored). Run scripts/create_producer_trigger_secret.sh
# afterward to store it as a Kubernetes Secret.
#
# Usage: scripts/generate_producer_trigger_token.sh [out-file]
set -euo pipefail

cd "$(dirname "$0")/.."

OUT_FILE="${1:-secrets/producer-trigger-token.b64}"

if [[ -f "$OUT_FILE" ]]; then
  echo "error: $OUT_FILE already exists; remove it first if you want a new token" >&2
  exit 1
fi

mkdir -p "$(dirname "$OUT_FILE")"
# No trailing newline: `kubectl create secret --from-file` stores the raw
# file bytes, so a trailing newline here would end up baked into the
# Secret value and mismatch a token read back via `$(cat ...)` (command
# substitution strips trailing newlines).
printf '%s' "$(openssl rand -hex 32)" > "$OUT_FILE"
chmod 600 "$OUT_FILE"
echo "Generated trigger token -> $OUT_FILE (keep this secret; it is git-ignored)"
