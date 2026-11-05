#!/bin/bash
# Run `just` from a container image instead of the host binary.
#
# Host `just` older than 1.31 rejects the `[positional-arguments]` attribute, so
# `just --evaluate` (the local `justfile-check` gate) cannot run there. This
# helper reuses the published Telemachy CI image rather than building one.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
MINIMUM_JUST="1.31"

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

# An explicit image is authoritative; otherwise prefer the local development
# image, then the artifact AchaeanFleet publishes, then the fleet CI image.
if [ -n "${TELEMACHY_JUST_IMAGE:-}" ]; then
    images=("${TELEMACHY_JUST_IMAGE}")
else
    images=("telemachy-ci:local" "${TELEMACHY_CI_IMAGE:-ghcr.io/homericintelligence/achaean-telemachy-ci:ci}" "localhost/hephaestus-ci:verify")
fi

if [ -z "${TELEMACHY_JUST_IMAGE:-}" ]; then
    published="${TELEMACHY_CI_IMAGE:-ghcr.io/homericintelligence/achaean-telemachy-ci:ci}"
    if ! "${CONTAINER_ENGINE}" image inspect "${published}" >/dev/null 2>&1; then
        if [ -n "${GHCR_TOKEN:-}" ]; then
            printf '%s\n' "${GHCR_TOKEN}" | "${CONTAINER_ENGINE}" login ghcr.io \
                --username "${GHCR_ACTOR:-git}" --password-stdin >/dev/null
        fi
        "${CONTAINER_ENGINE}" pull "${published}" >/dev/null 2>&1 \
            || echo "Could not pull ${published} (private package: export GHCR_TOKEN); falling back to a local image." >&2
    fi
fi

selected=
for image in "${images[@]}"; do
    if ! "${CONTAINER_ENGINE}" image inspect "${image}" >/dev/null 2>&1; then
        continue
    fi
    if ! output=$("${CONTAINER_ENGINE}" run --rm "${image}" just --version 2>/dev/null); then
        continue
    fi
    version="${output##* }"
    if [ "$(printf '%s\n%s\n' "${MINIMUM_JUST}" "${version}" | sort -V | tail -n 1)" = "${version}" ]; then
        selected="${image}"
        echo "Using ${selected} (just ${version})" >&2
        break
    fi
done

if [ -z "${selected}" ]; then
    echo "No local image with just >= ${MINIMUM_JUST}: ${images[*]}" >&2
    echo "Pull one with 'just ci-pull' or set TELEMACHY_JUST_IMAGE." >&2
    exit 1
fi

user_args=(--userns "keep-id:uid=1000,gid=1000")
if [ "$(basename "${CONTAINER_ENGINE}")" = docker ]; then
    user_args=(--user "$(id -u):$(id -g)")
fi

if [ "$#" -eq 0 ]; then
    set -- --evaluate
fi

exec "${CONTAINER_ENGINE}" run --rm "${user_args[@]}" \
    -v "${PROJECT_ROOT}:/workspace:Z" -w /workspace \
    "${selected}" just "$@"
