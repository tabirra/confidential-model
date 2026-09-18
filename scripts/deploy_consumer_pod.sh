#!/usr/bin/env bash
# Renders k8s/consumer-pod.yaml (a template with ${HF_REPO_ID}/${HF_MODEL_ID}
# placeholders) and applies it, instead of hand-editing the manifest.
#
# Usage: HF_USERNAME=<hf-username> scripts/deploy_consumer_pod.sh
#    or: HF_REPO_ID=<user>/<repo> [HF_MODEL_ID=<model-id>] scripts/deploy_consumer_pod.sh
#
# This only covers the Layer 1 manifest (k8s/consumer-pod.yaml); the Layer
# 3 attested-release manifest (k8s/consumer-pod-coco.yaml) still needs its
# KBS_RESOURCE_PATH and aa_kbc_params annotation edited by hand.
set -euo pipefail

cd "$(dirname "$0")/.."

export HF_MODEL_ID="${HF_MODEL_ID:-prajjwal1/bert-tiny}"

if [[ -z "${HF_REPO_ID:-}" ]]; then
  if [[ -z "${HF_USERNAME:-}" ]]; then
    echo "error: set HF_USERNAME (or HF_REPO_ID directly) before running this script" >&2
    exit 1
  fi
  export HF_REPO_ID="${HF_USERNAME}/bert-tiny-encrypted"
fi

envsubst '${HF_REPO_ID} ${HF_MODEL_ID}' < k8s/consumer-pod.yaml | kubectl apply -f -
echo "Deployed confidential-model-consumer with HF_REPO_ID=$HF_REPO_ID HF_MODEL_ID=$HF_MODEL_ID"
