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
# itself, so it must be in a public registry with publicly-trusted TLS. Any
# registry works (ghcr.io, quay.io, docker.io, a self-hosted one with a
# trusted certificate). Instead of the full image you can set
# CONSUMER_REGISTRY=<registry>/<namespace> [CONSUMER_TAG=<tag>] and the image
# becomes <registry>/<namespace>/confidential-model-consumer:<tag>.
#
# Add --publish (or PUBLISH_IMAGE=1) to build and push the consumer image
# first, as part of the same run, via scripts/publish_consumer_image.sh
# (which also verifies the image can be pulled anonymously). It needs a
# prior `docker login <registry>`:
#   CONSUMER_REGISTRY=quay.io/<org> HF_USERNAME=<hf-username> \
#     scripts/deploy_consumer_pod.sh coco --publish
# KBS_NAMESPACE must match whatever scripts/deploy_coco_kbs.sh printed for
# your cluster (it defaults to coco-tenant, not the CoCo operator's own
# confidential-containers-system namespace). The KBS_RESOURCE_PATH the pod
# fetches from must separately match the --path used with
# scripts/push_key_to_kbs.sh (both default to default/key/my-model).
#
# HF_USERNAME, HF_REPO_ID, HF_MODEL_ID, KBS_NAMESPACE, CONSUMER_IMAGE,
# CONSUMER_REGISTRY and CONSUMER_TAG can also be set in a git-ignored .env file at the repo root (KEY=value lines).
# Variables already set in the environment take precedence over .env.
set -euo pipefail

cd "$(dirname "$0")/.."

# shellcheck source=lib/load_env.sh
source scripts/lib/load_env.sh
load_env_defaults HF_USERNAME HF_REPO_ID HF_MODEL_ID KBS_NAMESPACE CONSUMER_IMAGE CONSUMER_REGISTRY CONSUMER_TAG
resolve_consumer_image

MODE="layer1"
PUBLISH="${PUBLISH_IMAGE:-0}"
for arg in "$@"; do
  case "$arg" in
    coco) MODE="coco" ;;
    --publish) PUBLISH=1 ;;
    *) echo "error: unknown argument '$arg' (expected: coco, --publish)" >&2; exit 1 ;;
  esac
done
if [[ "$PUBLISH" == "1" && "$MODE" != "coco" ]]; then
  echo "error: --publish / PUBLISH_IMAGE=1 only applies to coco mode (Layer 1 loads the image into the local cluster; see scripts/build_consumer_image.sh)" >&2
  exit 1
fi

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
    echo "error: set CONSUMER_IMAGE (e.g. ghcr.io/<user>/confidential-model-consumer:latest) or CONSUMER_REGISTRY (e.g. ghcr.io/<user>) — the Kata guest pulls it from a public registry" >&2
    exit 1
  fi
  if [[ "$PUBLISH" == "1" ]]; then
    scripts/publish_consumer_image.sh "$CONSUMER_IMAGE"
  fi
  envsubst '${HF_REPO_ID} ${KBS_NAMESPACE} ${CONSUMER_IMAGE}' < k8s/consumer-pod-coco.yaml | kubectl apply -f -
  echo "Deployed confidential-model-consumer-coco with HF_REPO_ID=$HF_REPO_ID KBS_NAMESPACE=$KBS_NAMESPACE CONSUMER_IMAGE=$CONSUMER_IMAGE"
else
  envsubst '${HF_REPO_ID} ${HF_MODEL_ID}' < k8s/consumer-pod.yaml | kubectl apply -f -
  echo "Deployed confidential-model-consumer with HF_REPO_ID=$HF_REPO_ID HF_MODEL_ID=$HF_MODEL_ID"
fi
