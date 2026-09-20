#!/usr/bin/env bash
# Renders a consumer pod manifest (a template with ${HF_REPO_ID}/etc.
# placeholders) and applies it, instead of hand-editing the manifest.
#
# Usage: HF_USERNAME=<hf-username> scripts/deploy_consumer_pod.sh
#    or: HF_REPO_ID=<user>/<repo> [HF_MODEL_ID=<model-id>] scripts/deploy_consumer_pod.sh
#
# Deploys k8s/consumer-pod.yaml (Layer 1, Secret-based key delivery) by
# default. Pass "coco" as the first argument to deploy
# k8s/consumer-pod-coco.yaml instead (Layer 3, KBS-attested key delivery):
#   HF_USERNAME=<hf-username> KBS_NAMESPACE=coco-tenant \
#   CONSUMER_IMAGE=ghcr.io/<user>/confidential-model-consumer:latest \
#     scripts/deploy_consumer_pod.sh coco
# CONSUMER_IMAGE is required in coco mode: the Kata guest VM pulls the image
# itself, so it must be in a public registry with publicly-trusted TLS.
# KBS_NAMESPACE must match whatever scripts/deploy_coco_kbs.sh printed for
# your cluster (it defaults to coco-tenant, not the CoCo operator's own
# confidential-containers-system namespace). The KBS_RESOURCE_PATH the pod
# fetches from must separately match the --path used with
# scripts/push_key_to_kbs.sh (both default to default/key/my-model).
set -euo pipefail

cd "$(dirname "$0")/.."

MODE="${1:-layer1}"

export HF_MODEL_ID="${HF_MODEL_ID:-prajjwal1/bert-tiny}"

if [[ -z "${HF_REPO_ID:-}" ]]; then
  if [[ -z "${HF_USERNAME:-}" ]]; then
    echo "error: set HF_USERNAME (or HF_REPO_ID directly) before running this script" >&2
    exit 1
  fi
  export HF_REPO_ID="${HF_USERNAME}/bert-tiny-encrypted"
fi

if [[ "$MODE" == "coco" ]]; then
  export KBS_NAMESPACE="${KBS_NAMESPACE:-coco-tenant}"
  if [[ -z "${CONSUMER_IMAGE:-}" ]]; then
    echo "error: set CONSUMER_IMAGE (e.g. ghcr.io/<user>/confidential-model-consumer:latest) — the Kata guest pulls it from a public registry" >&2
    exit 1
  fi
  envsubst '${HF_REPO_ID} ${KBS_NAMESPACE} ${CONSUMER_IMAGE}' < k8s/consumer-pod-coco.yaml | kubectl apply -f -
  echo "Deployed confidential-model-consumer-coco with HF_REPO_ID=$HF_REPO_ID KBS_NAMESPACE=$KBS_NAMESPACE CONSUMER_IMAGE=$CONSUMER_IMAGE"
else
  envsubst '${HF_REPO_ID} ${HF_MODEL_ID}' < k8s/consumer-pod.yaml | kubectl apply -f -
  echo "Deployed confidential-model-consumer with HF_REPO_ID=$HF_REPO_ID HF_MODEL_ID=$HF_MODEL_ID"
fi
