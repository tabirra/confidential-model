#!/usr/bin/env bash
# Renders and applies k8s/producer-pod.yaml — the producer's optional
# trigger-server mode (see that file and README.md). Also applies the
# RBAC it needs (k8s/producer-rbac.yaml, idempotent). Requires the
# producer-trigger-token Secret to already exist
# (scripts/generate_producer_trigger_token.sh +
# scripts/create_producer_trigger_secret.sh).
#
# Usage: HF_USERNAME=<hf-username> scripts/deploy_producer_pod.sh
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ -z "${HF_USERNAME:-}" ]]; then
  echo "error: set HF_USERNAME before running this script" >&2
  exit 1
fi
export HF_USERNAME
export HF_MODEL_ID="${HF_MODEL_ID:-prajjwal1/bert-tiny}"

kubectl apply -f k8s/producer-rbac.yaml
envsubst '${HF_USERNAME} ${HF_MODEL_ID}' < k8s/producer-pod.yaml | kubectl apply -f -

echo "Deployed confidential-model-producer-trigger with HF_USERNAME=$HF_USERNAME HF_MODEL_ID=$HF_MODEL_ID"
echo
echo "Trigger it with:"
echo "  kubectl port-forward pod/confidential-model-producer-trigger 8080:8080 &"
echo "  curl -X POST --max-time 300 \\"
echo "    -H \"Authorization: Bearer \$(cat secrets/producer-trigger-token.b64)\" \\"
echo "    http://127.0.0.1:8080/mermelada"
