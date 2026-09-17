#!/usr/bin/env bash
# Deploys the Confidential Containers (CoCo) operator and Trustee KBS on
# the current kubectl context, wiring up the "kata-qemu-coco-dev" runtime
# class so pods can opt into attested key release (Layer 3).
#
# This wraps the general public CoCo install flow. Exact manifest paths
# and kustomize overlays move between CoCo/Trustee releases, so treat this
# as a starting point: pin OPERATOR_REF/TRUSTEE_REF to versions you've
# validated, and check `kubectl get pods -A` / operator logs if a step
# doesn't behave as expected on your cluster. Upstream docs:
#   https://github.com/confidential-containers/operator
#   https://github.com/confidential-containers/trustee
set -euo pipefail

OPERATOR_REF="${OPERATOR_REF:-main}"
TRUSTEE_REF="${TRUSTEE_REF:-main}"
NAMESPACE="${NAMESPACE:-confidential-containers-system}"

command -v kubectl >/dev/null 2>&1 || { echo "error: kubectl not found on PATH" >&2; exit 1; }

echo "== 1/4: cert-manager (CoCo operator dependency) =="
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/latest/download/cert-manager.yaml
kubectl wait --for=condition=Available --timeout=300s -n cert-manager deployment --all

echo "== 2/4: CoCo operator (ref=$OPERATOR_REF) =="
kubectl apply -k "github.com/confidential-containers/operator/config/default?ref=${OPERATOR_REF}"
kubectl wait --for=condition=Available --timeout=300s -n "$NAMESPACE" deployment --all

echo "== 3/4: ccruntime CR selecting the kata-qemu-coco-dev runtime class =="
cat <<EOF | kubectl apply -f -
apiVersion: confidentialcontainers.org/v1beta1
kind: CcRuntime
metadata:
  name: ccruntime-sample
  namespace: ${NAMESPACE}
spec:
  runtimeName: kata
  ccRuntimeConfig:
    runtimeClasses:
      - name: kata-qemu-coco-dev
        snapshotter: nydus
    installType: bundle
    installDoEtcHosts: "true"
EOF

echo "waiting for the kata-qemu-coco-dev RuntimeClass to appear ..."
for _ in $(seq 1 30); do
  kubectl get runtimeclass kata-qemu-coco-dev >/dev/null 2>&1 && break
  sleep 10
done
kubectl get runtimeclass kata-qemu-coco-dev

echo "== 4/4: Trustee KBS (ref=$TRUSTEE_REF), sample/dev deployment =="
kubectl apply -k "github.com/confidential-containers/trustee/kbs/config/kubernetes/base?ref=${TRUSTEE_REF}"
kubectl wait --for=condition=Available --timeout=300s -n "$NAMESPACE" deployment/kbs || true

cat <<'EOF'

Next steps:
  1. Fetch the KBS admin private key the deployment generated (name/path
     depends on the overlay used above — check the kbs Secret/ConfigMap in
     the confidential-containers-system namespace) and export it as
     KBS_AUTH_PRIVATE_KEY for scripts/push_key_to_kbs.sh and
     scripts/set_kbs_resource_policy.sh.
  2. Note the KBS Service's cluster address (e.g.
     http://kbs.confidential-containers-system.svc.cluster.local:8080) and
     export it as KBS_URL for the same two scripts.
  3. Run scripts/set_kbs_resource_policy.sh, then scripts/push_key_to_kbs.sh.
  4. Point k8s/consumer-pod-coco.yaml's
     io.katacontainers.config.agent.aa_kbc_params annotation at that same
     KBS_URL before applying it.
EOF
