# Sourced by scripts/*.sh (not executed). Provides load_env_defaults, which
# reads the named variables from the git-ignored .env at the repo root
# (KEY=value lines, single/double quotes stripped) WITHOUT overriding
# variables already set in the environment. Only the names passed in are
# read, so unrelated entries such as HF_TOKEN never leak into child processes.
# Callers must already be in the repo root.
load_env_defaults() {
  [[ -f .env ]] || return 0
  local names
  names="$(IFS='|'; echo "$*")"
  local line key val
  while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ "$line" =~ ^[[:space:]]*(${names})=(.*)$ ]]; then
      key="${BASH_REMATCH[1]}"
      val="${BASH_REMATCH[2]}"
      val="${val%\"}"; val="${val#\"}"; val="${val%\'}"; val="${val#\'}"
      [[ -n "${!key+x}" ]] || export "$key=$val"
    fi
  done < .env
}

# Sets CONSUMER_IMAGE when it isn't already set, from CONSUMER_REGISTRY (any
# registry + namespace, e.g. ghcr.io/<user>, quay.io/<org>, docker.io/<user>)
# and optional CONSUMER_TAG (default: latest). An explicit CONSUMER_IMAGE
# always wins.
resolve_consumer_image() {
  if [[ -z "${CONSUMER_IMAGE:-}" && -n "${CONSUMER_REGISTRY:-}" ]]; then
    export CONSUMER_IMAGE="${CONSUMER_REGISTRY%/}/confidential-model-consumer:${CONSUMER_TAG:-latest}"
  fi
}
