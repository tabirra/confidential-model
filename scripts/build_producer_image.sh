#!/usr/bin/env bash
# Builds the producer image. Unlike the consumer, the producer doesn't run
# in the cluster, so there's no kind/minikube load step — just `docker run`
# it locally or in CI (see README.md for the full invocation).
set -euo pipefail

cd "$(dirname "$0")/.."

IMAGE="${1:-confidential-model-producer:latest}"

docker build -t "$IMAGE" -f producer/Dockerfile .
echo "built $IMAGE"
