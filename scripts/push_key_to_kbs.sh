#!/usr/bin/env bash
# Pushes the local decryption key into Trustee KBS as a resource, so the
# consumer pod can fetch it via attested release (Layer 3) instead of a
# Kubernetes Secret.
#
# Requires the `kbs-client` binary (built from
# https://github.com/confidential-containers/trustee) and a running KBS
# reachable at $KBS_URL, already deployed per
# scripts/deploy_coco_kbs.sh.
#
# Usage:
#   KBS_URL=http://<kbs-host>:8080 \
#   KBS_AUTH_PRIVATE_KEY=./kbs/kbs-admin.key \
#     scripts/push_key_to_kbs.sh [key-file] [resource-path]
#
# resource-path defaults to default/key/my-model, matching the layout
# kbs-client expects: <repository>/<type>/<tag>. This is also the value
# the consumer pod's KBS_RESOURCE_PATH env var (k8s/consumer-pod-coco.yaml)
# must be set to.
set -euo pipefail

KEY_FILE="${1:-secrets/decryption-key.b64}"
RESOURCE_PATH="${2:-default/key/my-model}"
KBS_URL="${KBS_URL:?set KBS_URL to the Trustee KBS address, e.g. http://kbs.default.svc.cluster.local:8080}"
KBS_AUTH_PRIVATE_KEY="${KBS_AUTH_PRIVATE_KEY:?set KBS_AUTH_PRIVATE_KEY to the KBS admin private key path}"

if [[ ! -f "$KEY_FILE" ]]; then
  echo "error: key file not found at $KEY_FILE" >&2
  echo "run producer/encrypt_and_push.py first" >&2
  exit 1
fi

if ! command -v kbs-client >/dev/null 2>&1; then
  echo "error: kbs-client not found on PATH" >&2
  echo "build it from https://github.com/confidential-containers/trustee (attestation-service/../kbs/tools/client)" >&2
  exit 1
fi

echo "pushing $KEY_FILE -> $KBS_URL resource '$RESOURCE_PATH'"
kbs-client --url "$KBS_URL" config --auth-private-key "$KBS_AUTH_PRIVATE_KEY"
kbs-client --url "$KBS_URL" set-resource \
  --path "$RESOURCE_PATH" \
  --resource-file "$KEY_FILE"

echo "done. Consumer pods should set KBS_RESOURCE_PATH=$RESOURCE_PATH to fetch this key via CDH."
echo "note: exact kbs-client subcommand flags vary by Trustee version — run 'kbs-client --help' / 'kbs-client set-resource --help' to confirm against what you have installed."
