#!/usr/bin/env bash
# Generates a KBS admin bearer JWT signed with an Ed25519 private key, for
# use with `kbs-client config --admin-token-file <token>`. KBS verifies
# this against the admin public key it was deployed with (kbs.pem, see
# scripts/deploy_coco_kbs.sh) and requires a `role: admin` claim, per this
# deployment's kbs-config.toml [admin.authorization.regex_acl] rule.
#
# Usage: scripts/generate_kbs_admin_token.sh [private-key-file] [output-file] [validity-seconds]
set -euo pipefail

cd "$(dirname "$0")/.."

KEY_FILE="${1:-kbs/kbs-admin.key}"
OUT_FILE="${2:-kbs/admin-token}"
VALIDITY_SECS="${3:-315360000}" # ~10 years, matching Trustee's own quickstart example

if [[ ! -f "$KEY_FILE" ]]; then
  echo "error: private key not found at $KEY_FILE" >&2
  echo "generate one with: openssl genpkey -algorithm ed25519 -out $KEY_FILE" >&2
  exit 1
fi

b64url() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }

header='{"alg":"EdDSA","typ":"JWT"}'
iat=$(date +%s)
exp=$((iat + VALIDITY_SECS))
payload="{\"issuer\":\"admin\",\"subject\":\"admin\",\"role\":\"admin\",\"audiences\":[],\"iat\":${iat},\"exp\":${exp}}"

h64=$(printf '%s' "$header" | b64url)
p64=$(printf '%s' "$payload" | b64url)

# openssl pkeyutl -rawin needs a seekable input, not a pipe.
signing_input="$(mktemp)"
trap 'rm -f "$signing_input"' EXIT
printf '%s.%s' "$h64" "$p64" > "$signing_input"
sig=$(openssl pkeyutl -sign -inkey "$KEY_FILE" -rawin -in "$signing_input" | b64url)

printf '%s.%s.%s\n' "$h64" "$p64" "$sig" > "$OUT_FILE"
echo "wrote admin token to $OUT_FILE (valid ${VALIDITY_SECS}s)"
