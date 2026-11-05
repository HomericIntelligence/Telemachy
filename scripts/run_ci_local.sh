#!/bin/bash
# Local and hosted checks share ci/checks.py and the fleet CI image published
# by AchaeanFleet. The default subset runs every check.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SUBSET="${1:-all}"
IMAGE="${TELEMACHY_CI_IMAGE:-ghcr.io/homericintelligence/achaean-telemachy-ci:ci}"

if [ -z "${CONTAINER_ENGINE:-}" ]; then
    if command -v podman >/dev/null; then
        CONTAINER_ENGINE=podman
    elif command -v docker >/dev/null; then
        CONTAINER_ENGINE=docker
    else
        echo 'No container engine found; install Podman or Docker.' >&2
        exit 1
    fi
fi
command -v "${CONTAINER_ENGINE}" >/dev/null

# The image is built, verified, and published by AchaeanFleet. A missing local
# copy is pulled once; an unauthenticated pull works for the public package.
pull_image() {
    if "${CONTAINER_ENGINE}" image inspect "${IMAGE}" >/dev/null 2>&1; then
        return
    fi
    token="${GHCR_TOKEN:-${GITHUB_TOKEN:-}}"
    if [ -n "${token}" ]; then
        printf '%s\n' "${token}" | "${CONTAINER_ENGINE}" login ghcr.io \
            --username "${GHCR_ACTOR:-${GITHUB_ACTOR:-git}}" \
            --password-stdin >/dev/null
    fi
    "${CONTAINER_ENGINE}" pull "${IMAGE}" || {
        echo "Could not pull ${IMAGE}." >&2
        echo "AchaeanFleet publishes this package publicly; see its telemachy-ci workflow." >&2
        exit 1
    }
}

if [ "${SUBSET}" = "pull" ]; then
    pull_image
    echo "Using ${IMAGE}"
    exit 0
fi
pull_image

architecture=$("${CONTAINER_ENGINE}" image inspect --format '{{.Architecture}}' "${IMAGE}")
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

exec "${CONTAINER_ENGINE}" run --rm "${user_args[@]}" \
    --cpus "${TELEMACHY_CI_CPUS:-2}" --memory "${TELEMACHY_CI_MEMORY:-4g}" --pids-limit 512 \
    "${volume_args[@]}" \
    -v "${ci_environment}:/workspace/.pixi:Z" \
    -v "${ci_home}:/home/ci:Z" \
    -v "${ci_cache}:/home/ci/.cache/pixi:Z" -w /workspace \
    "${IMAGE}" bash -c 'exec python3 ci/checks.py "$@"' _ "${SUBSET}"
