#!/usr/bin/env bash
# Builds the consumer image and pushes it to a public registry, then checks
# that it can be pulled anonymously over publicly-trusted TLS — which is what
# the Kata guest VM needs for guest-pull (Layer 3): it pulls the workload
# image itself, so a local/minikube-loaded image, a private package or a
# self-signed registry all fail.
#
# Usage: scripts/publish_consumer_image.sh [<image>]
#   <image> defaults to $CONSUMER_IMAGE (environment or .env), e.g.
#   ghcr.io/<user>/confidential-model-consumer:latest
#
# Requires `docker login <registry>` beforehand (for ghcr.io: a classic PAT
# with write:packages). On ghcr.io a newly created package is PRIVATE and can
# only be made public from the GitHub web UI (Package settings -> Danger Zone
# -> Change visibility); the anonymous-pull check at the end tells you when
# that is still pending — make it public, then re-run this script.
#
# Afterwards deploy with:
#   CONSUMER_IMAGE=<image> scripts/deploy_consumer_pod.sh coco
set -euo pipefail

cd "$(dirname "$0")/.."

# shellcheck source=lib/load_env.sh
source scripts/lib/load_env.sh
load_env_defaults CONSUMER_IMAGE

IMAGE="${1:-${CONSUMER_IMAGE:-}}"
if [[ -z "$IMAGE" ]]; then
  echo "error: pass the image as an argument or set CONSUMER_IMAGE (e.g. ghcr.io/<user>/confidential-model-consumer:latest)" >&2
  exit 1
fi

# Split "[host/]repo[:tag]" into registry host, repository path and tag.
# A first component without '.' or ':' (and not "localhost") is a Docker Hub
# user, e.g. "user/img" -> docker.io.
ref="$IMAGE"
tag="latest"
last="${ref##*/}"
if [[ "$last" == *:* ]]; then
  tag="${last##*:}"
  ref="${ref%:*}"
fi
first="${ref%%/*}"
if [[ "$ref" == */* && ( "$first" == *.* || "$first" == *:* || "$first" == localhost ) ]]; then
  host="$first"
  repo="${ref#*/}"
else
  host="docker.io"
  repo="$ref"
  [[ "$repo" == */* ]] || repo="library/$repo"
fi

case "$host" in
  localhost|localhost:*|127.*|*.local|*.svc|*.svc.cluster.local)
    echo "error: '$host' is not a public registry; the Kata guest can't pull from it. Use e.g. ghcr.io/<user>/..." >&2
    exit 1
    ;;
esac
if [[ "$repo" != */* ]]; then
  echo "error: '$IMAGE' has no namespace; use <registry>/<user-or-org>/<name>[:tag]" >&2
  exit 1
fi

echo "==> building $IMAGE"
docker build -t "$IMAGE" -f consumer/Dockerfile .

echo "==> pushing $IMAGE"
if ! docker push "$IMAGE"; then
  echo "error: push failed; run 'docker login $host' first (ghcr.io needs a classic PAT with write:packages)" >&2
  exit 1
fi

size_mb=$(( $(docker image inspect "$IMAGE" --format '{{.Size}}') / 1024 / 1024 ))

# Anonymous pull check against the registry HTTP API (registry v2 token
# flow, no credentials). curl validates TLS against the system CA store, so a
# self-signed registry fails here too.
api_host="$host"
[[ "$host" == "docker.io" ]] && api_host="registry-1.docker.io"
url="https://${api_host}/v2/${repo}/manifests/${tag}"
accept='application/vnd.oci.image.index.v1+json,application/vnd.oci.image.manifest.v1+json,application/vnd.docker.distribution.manifest.list.v2+json,application/vnd.docker.distribution.manifest.v2+json'

echo "==> checking anonymous pull of $url"
status="$(curl -sS -o /dev/null -w '%{http_code}' -H "Accept: $accept" "$url" || true)"
if [[ "$status" == "401" ]]; then
  challenge="$(curl -sSI -H "Accept: $accept" "$url" | tr -d '\r' | grep -i '^www-authenticate:' || true)"
  realm="$(sed -n 's/.*realm="\([^"]*\)".*/\1/p' <<<"$challenge")"
  service="$(sed -n 's/.*service="\([^"]*\)".*/\1/p' <<<"$challenge")"
  if [[ -n "$realm" ]]; then
    token="$(curl -sS -G "$realm" --data-urlencode "service=$service" \
      --data-urlencode "scope=repository:${repo}:pull" \
      | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("token") or d.get("access_token") or "")' || true)"
    if [[ -n "$token" ]]; then
      status="$(curl -sS -o /dev/null -w '%{http_code}' -H "Accept: $accept" \
        -H "Authorization: Bearer $token" "$url" || true)"
    fi
  fi
fi

if [[ "$status" != "200" ]]; then
  echo "error: anonymous pull of $IMAGE failed (HTTP ${status:-none}); the Kata guest would not be able to pull it." >&2
  if [[ "$host" == "ghcr.io" ]]; then
    echo "  ghcr.io packages start private: open https://github.com/users/<user>/packages/container/${repo#*/}/settings" >&2
    echo "  (or your org's equivalent), set visibility to Public, then re-run this script." >&2
  else
    echo "  make the repository public on $host and re-run this script." >&2
  fi
  exit 1
fi

echo "OK: $IMAGE is publicly pullable."
echo "Unpacked size is about ${size_mb} MB: the Kata guest unpacks it into its own RAM, so"
echo "default_memory in configuration-qemu-coco-dev.toml must comfortably exceed that (see README)."
echo "Deploy with: CONSUMER_IMAGE=$IMAGE scripts/deploy_consumer_pod.sh coco"
