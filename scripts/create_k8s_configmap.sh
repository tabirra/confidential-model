#!/usr/bin/env bash
# Creates (or updates) the Kubernetes ConfigMap holding the trusted signing
# public key, from the local key file the producer wrote. Public keys
# aren't secret, but this is still delivered via a channel independent of
# the Hugging Face Hub repo that carries the artifact + signature (see
# k8s/configmap.example.yaml for why that separation matters).
#
# Usage: scripts/create_k8s_configmap.sh [public-key-file] [configmap-name] [namespace]
set -euo pipefail

cd "$(dirname "$0")/.."

PUBLIC_KEY_FILE="${1:-keys/signing-public-key.pem}"
CONFIGMAP_NAME="${2:-model-signing-public-key}"
NAMESPACE="${3:-default}"

if [[ ! -f "$PUBLIC_KEY_FILE" ]]; then
  echo "error: public key file not found at $PUBLIC_KEY_FILE" >&2
  echo "run producer/encrypt_and_push.py first" >&2
  exit 1
fi

kubectl create configmap "$CONFIGMAP_NAME" \
  --namespace "$NAMESPACE" \
  --from-file=public-key.pem="$PUBLIC_KEY_FILE" \
  --dry-run=client -o yaml | kubectl apply -f -

echo "ConfigMap '$CONFIGMAP_NAME' created/updated in namespace '$NAMESPACE'."
