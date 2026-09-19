#!/usr/bin/env bash
# Creates (or updates) the Kubernetes Secret holding the bearer token the
# producer's /mermelada endpoint checks requests against (see
# producer/server.py, k8s/producer-pod.yaml). Generate the token first
# with scripts/generate_producer_trigger_token.sh.
#
# Usage: scripts/create_producer_trigger_secret.sh [token-file] [secret-name] [namespace]
set -euo pipefail

cd "$(dirname "$0")/.."

TOKEN_FILE="${1:-secrets/producer-trigger-token.b64}"
SECRET_NAME="${2:-producer-trigger-token}"
NAMESPACE="${3:-default}"

if [[ ! -f "$TOKEN_FILE" ]]; then
  echo "error: token file not found at $TOKEN_FILE" >&2
  echo "run scripts/generate_producer_trigger_token.sh first" >&2
  exit 1
fi

kubectl create secret generic "$SECRET_NAME" \
  --namespace "$NAMESPACE" \
  --from-file=token="$TOKEN_FILE" \
  --dry-run=client -o yaml | kubectl apply -f -

echo "Secret '$SECRET_NAME' created/updated in namespace '$NAMESPACE'."
