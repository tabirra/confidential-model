#!/usr/bin/env bash
# Builds the consumer image and, if a local kind/minikube cluster is
# detected, loads it so k8s/consumer-pod.yaml can use imagePullPolicy: IfNotPresent
# without needing a registry push.
set -euo pipefail

cd "$(dirname "$0")/.."

IMAGE="${1:-confidential-model-consumer:latest}"

docker build -t "$IMAGE" -f consumer/Dockerfile .

if command -v kind >/dev/null 2>&1 && kind get clusters >/dev/null 2>&1; then
  cluster="$(kind get clusters | head -n1)"
  echo "loading $IMAGE into kind cluster '$cluster'"
  kind load docker-image "$IMAGE" --name "$cluster"
elif command -v minikube >/dev/null 2>&1 && minikube status >/dev/null 2>&1; then
  echo "loading $IMAGE into minikube"
  minikube image load "$IMAGE"
else
  echo "no local kind/minikube cluster detected; push $IMAGE to a registry your cluster can pull from instead"
fi
