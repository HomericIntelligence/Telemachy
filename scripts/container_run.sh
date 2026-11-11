#!/bin/bash
# Run any command inside the Telemachy CI image that AchaeanFleet publishes.
#
# CI jobs, the local CI suite, and the justfile recipes all enter here, so a
# command that passes on a laptop executes with the same image and toolchain
# as the required checks. The image is private: pulls authenticate with
# GHCR_TOKEN (a classic token with read:packages).
#
# Environment:
#   TELEMACHY_CI_IMAGE   Image reference override (default: published :ci tag)
#   TELEMACHY_HOST=1     Run the command on the host instead of the image
#   TELEMACHY_CONTAINER_NETWORK  Engine network mode (default: host for the
#                       workflow commands that call Agamemnon and NATS)
#   GHCR_TOKEN, GHCR_ACTOR       Pull credential and its registry username
#   TELEMACHY_CI_CPUS, TELEMACHY_CI_MEMORY  Operator budgets
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PUBLISHED_IMAGE="${TELEMACHY_CI_IMAGE:-ghcr.io/homericintelligence/achaean-telemachy-ci:ci}"
LOCAL_IMAGE="telemachy-ci:local"

# Recipes pass engine-neutral flags before the command; anything else is it.
NETWORK_ARGS=()
PULL_ONLY=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --network)
            NETWORK_ARGS=(--network "${2:?--network requires a mode}")
            shift 2
            ;;
        --pull)
            PULL_ONLY=1
            shift
            ;;
        --)
            shift
            break
            ;;
        *)
            break
            ;;
    esac
done

# The documented host escape hatch keeps offline and host-only use working
# without leaving the justfile recipes.
if [ "${TELEMACHY_HOST:-0}" = "1" ]; then
    if [ "$#" -eq 0 ]; then
        echo 'container_run.sh requires a command' >&2
        exit 2
    fi
    exec "$@"
fi

if [ "$#" -eq 0 ] && [ "${PULL_ONLY}" -eq 0 ]; then
    echo 'container_run.sh requires a command, for example: bash scripts/container_run.sh true' >&2
    exit 2
fi

if [ -z "${CONTAINER_ENGINE:-}" ]; then
    if command -v podman >/dev/null; then
        CONTAINER_ENGINE=podman
    elif command -v docker >/dev/null; then
        CONTAINER_ENGINE=docker
    else
        echo 'No container engine found; install Podman or Docker, or set TELEMACHY_HOST=1.' >&2
        exit 1
    fi
fi
command -v "${CONTAINER_ENGINE}" >/dev/null

engine_has_image() {
    "${CONTAINER_ENGINE}" image inspect "$1" >/dev/null 2>&1
}

# A local build of the recipe takes precedence so a recipe change can be
# exercised before AchaeanFleet publishes it.
image="${PUBLISHED_IMAGE}"
if [ -z "${TELEMACHY_CI_IMAGE:-}" ] && engine_has_image "${LOCAL_IMAGE}"; then
    image="${LOCAL_IMAGE}"
fi

pull_image() {
    if engine_has_image "${image}"; then
        return
    fi
    if [ -n "${GHCR_TOKEN:-}" ]; then
        printf '%s\n' "${GHCR_TOKEN}" | "${CONTAINER_ENGINE}" login ghcr.io \
            --username "${GHCR_ACTOR:-git}" --password-stdin >/dev/null
    fi
    "${CONTAINER_ENGINE}" pull "${image}" || {
        echo "Could not pull ${image}." >&2
        echo "The package is private because organization policy forbids public" >&2
        echo "container packages; pulls need GHCR_TOKEN with read:packages." >&2
        exit 1
    }
}

pull_image
if [ "${PULL_ONLY}" -eq 1 ]; then
    echo "Using ${image}"
    exit 0
fi

architecture=$("${CONTAINER_ENGINE}" image inspect --format '{{.Architecture}}' "${image}")
case "${architecture}" in
    amd64|arm64) ;;
    *) echo "Unsupported CI image architecture: ${architecture}" >&2; exit 1 ;;
esac

# A Linux environment must never delete or reuse the host's .pixi. Keep each
# image architecture in its own ignored directory at the stable container path.
ci_environment="${PROJECT_ROOT}/.ci-pixi/${architecture}"
ci_home="${PROJECT_ROOT}/.ci-pixi/${architecture}-home"
ci_cache="${HOME}/.cache/pixi"
mkdir -p "${ci_environment}" "${ci_home}/.cache" "${ci_cache}"
user_args=("--userns=keep-id:uid=1000,gid=1000")
if [ "$(basename "${CONTAINER_ENGINE}")" = docker ]; then
    user_args=(--user "$(id -u):$(id -g)")
fi
volume_args=(-v "${PROJECT_ROOT}:/workspace:Z")
if [ -f "${PROJECT_ROOT}/.git" ]; then
    # Worktree pointers name metadata outside the source mount. Expose only
    # that directory read-only; global Git overrides would leak into tests.
    git_common=$(git -C "${PROJECT_ROOT}" rev-parse --path-format=absolute --git-common-dir)
    volume_args+=(-v "${git_common}:${git_common}:ro")
fi

# Pass the documented settings by name so values never appear in the process
# list. Unset variables stay unset inside the container.
env_args=()
for name in \
    AGAMEMNON_URL AGAMEMNON_API_KEY AGAMEMNON_RATE_LIMIT_RPS AGAMEMNON_RATE_LIMIT_BURST \
    NATS_URL NATS_CLIENT_TOKEN GITHUB_TOKEN GITHUB_REPOSITORY HOST_ID REQUIRE_TLS \
    LOG_LEVEL LOG_FORMAT METRICS_ENABLED METRICS_PORT OTEL_ENABLED OTEL_SERVICE_NAME \
    OTEL_EXPORTER TELEMACHY_STATE_DIR STATE_DIR AUDIT_LOG_PATH AUDIT_HASH_CHAIN \
    MONITOR_TIMEOUT_SECONDS MONITOR_MAX_POLLS DEFAULT_WORKFLOW_TIMEOUT \
    HEALTHCHECK_INTERVAL_SECONDS HEALTHCHECK_FAILURE_THRESHOLD HEALTHCHECK_TIMEOUT_SECONDS \
    WORKFLOWS_DIR TELEMACHY_TEST_NATS_SERVER
do
    if [ -n "${!name:-}" ]; then
        env_args+=(-e "${name}")
    fi
done

# Workflow commands reach Agamemnon and NATS on the host loopback, so they
# request the host network explicitly; check commands stay isolated.
if [ "${#NETWORK_ARGS[@]}" -eq 0 ] && [ "${TELEMACHY_CONTAINER_NETWORK:-}" != "" ]; then
    NETWORK_ARGS=(--network "${TELEMACHY_CONTAINER_NETWORK}")
fi

exec "${CONTAINER_ENGINE}" run --rm "${user_args[@]}" \
    --cpus "${TELEMACHY_CI_CPUS:-2}" --memory "${TELEMACHY_CI_MEMORY:-4g}" --pids-limit 512 \
    "${volume_args[@]}" \
    -v "${ci_environment}:/workspace/.pixi:Z" \
    -v "${ci_home}:/home/ci:Z" \
    -v "${ci_cache}:/home/ci/.cache/pixi:Z" -w /workspace \
    "${NETWORK_ARGS[@]}" "${env_args[@]}" \
    "${image}" bash -c 'exec "$@"' _ "$@"
