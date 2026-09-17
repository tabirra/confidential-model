#!/usr/bin/env bash
# Uploads kbs/resource-policy.rego to Trustee KBS as the active resource
# policy, so KBS knows under what attestation conditions it may release
# resources (e.g. the model decryption key at default/key/my-model).
#
# Requires the `kbs-client` binary and a running KBS, same as
# scripts/push_key_to_kbs.sh.
#
# Usage:
#   KBS_URL=http://<kbs-host>:8080 \
#   KBS_AUTH_PRIVATE_KEY=./kbs/kbs-admin.key \
#     scripts/set_kbs_resource_policy.sh [policy-file]
set -euo pipefail

POLICY_FILE="${1:-kbs/resource-policy.rego}"
KBS_URL="${KBS_URL:?set KBS_URL to the Trustee KBS address, e.g. http://kbs.default.svc.cluster.local:8080}"
KBS_AUTH_PRIVATE_KEY="${KBS_AUTH_PRIVATE_KEY:?set KBS_AUTH_PRIVATE_KEY to the KBS admin private key path}"

if [[ ! -f "$POLICY_FILE" ]]; then
  echo "error: policy file not found at $POLICY_FILE" >&2
  exit 1
fi

if ! command -v kbs-client >/dev/null 2>&1; then
  echo "error: kbs-client not found on PATH" >&2
  echo "build it from https://github.com/confidential-containers/trustee" >&2
  exit 1
fi

echo "uploading $POLICY_FILE as the KBS resource policy at $KBS_URL"
kbs-client --url "$KBS_URL" config --auth-private-key "$KBS_AUTH_PRIVATE_KEY"
kbs-client --url "$KBS_URL" set-resource-policy --policy-file "$POLICY_FILE"

echo "done."
echo "note: exact kbs-client subcommand flags vary by Trustee version — run 'kbs-client --help' to confirm against what you have installed."
