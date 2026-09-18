#!/usr/bin/env bash
# Pushes the local decryption key into Trustee KBS as a resource, so the
# consumer pod can fetch it via attested release (Layer 3) instead of a
# Kubernetes Secret.
#
# Requires the `kbs-client` binary (built from
# https://github.com/confidential-containers/trustee, tools/kbs-client:
# `cargo build --release -p kbs-client`) and a running KBS reachable at
# $KBS_URL, already deployed per scripts/deploy_coco_kbs.sh.
#
# Usage:
#   KBS_URL=http://<kbs-host>:8080 \
#   KBS_ADMIN_TOKEN_FILE=kbs/admin-token \
#     scripts/push_key_to_kbs.sh [key-file] [resource-path]
#
# Generate KBS_ADMIN_TOKEN_FILE with scripts/generate_kbs_admin_token.sh
# (signed with the same private key whose public half was deployed as
# KBS's admin auth key in scripts/deploy_coco_kbs.sh).
#
# resource-path defaults to default/key/my-model, matching the layout
# kbs-client expects: <repository>/<type>/<tag>. This is also the value
# the consumer pod's KBS_RESOURCE_PATH env var (k8s/consumer-pod-coco.yaml)
# must be set to.
set -euo pipefail

KEY_FILE="${1:-secrets/decryption-key.b64}"
RESOURCE_PATH="${2:-default/key/my-model}"
KBS_URL="${KBS_URL:?set KBS_URL to the Trustee KBS address, e.g. http://kbs.coco-tenant.svc.cluster.local:8080}"
KBS_ADMIN_TOKEN_FILE="${KBS_ADMIN_TOKEN_FILE:?set KBS_ADMIN_TOKEN_FILE to a token from scripts/generate_kbs_admin_token.sh}"

if [[ ! -f "$KEY_FILE" ]]; then
  echo "error: key file not found at $KEY_FILE" >&2
  echo "run producer/encrypt_and_push.py first" >&2
  exit 1
fi

if ! command -v kbs-client >/dev/null 2>&1; then
  echo "error: kbs-client not found on PATH" >&2
  echo "build it from https://github.com/confidential-containers/trustee (tools/kbs-client)" >&2
  exit 1
fi

echo "pushing $KEY_FILE -> $KBS_URL resource '$RESOURCE_PATH'"
kbs-client --url "$KBS_URL" config --admin-token-file "$KBS_ADMIN_TOKEN_FILE" \
  set-resource --resource-file "$KEY_FILE" --path "$RESOURCE_PATH"

echo "done. Consumer pods should set KBS_RESOURCE_PATH=$RESOURCE_PATH to fetch this key via CDH."
echo "note: exact kbs-client subcommand flags vary by Trustee version — run 'kbs-client --help' / 'kbs-client config set-resource --help' to confirm against what you have installed."
