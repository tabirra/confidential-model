#!/usr/bin/env bash
# Creates (or updates) the Kubernetes Secret holding the model decryption
# key, from the local key file the producer wrote.
#
# Usage: scripts/create_k8s_secret.sh [key-file] [secret-name] [namespace]
set -euo pipefail

KEY_FILE="${1:-secrets/decryption-key.b64}"
SECRET_NAME="${2:-model-decryption-key}"
NAMESPACE="${3:-default}"

if [[ ! -f "$KEY_FILE" ]]; then
  echo "error: key file not found at $KEY_FILE" >&2
  echo "run producer/encrypt_and_push.py first" >&2
  exit 1
fi

kubectl create secret generic "$SECRET_NAME" \
  --namespace "$NAMESPACE" \
  --from-file=key="$KEY_FILE" \
  --dry-run=client -o yaml | kubectl apply -f -

echo "Secret '$SECRET_NAME' created/updated in namespace '$NAMESPACE'."
