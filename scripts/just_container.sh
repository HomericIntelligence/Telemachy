#!/bin/bash
# Run `just` from a container image instead of the host binary.
#
# Host `just` older than 1.31 rejects the `[positional-arguments]` attribute, so
# `just --evaluate` (the local `justfile-check` gate) cannot run there. This
# helper reuses an existing CI image that already carries a capable toolchain
# rather than building or pulling anything.
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

# An explicit image is authoritative; otherwise reuse the declared CI image and
# the fleet CI image that developers already have on disk.
if [ -n "${TELEMACHY_JUST_IMAGE:-}" ]; then
    images=("${TELEMACHY_JUST_IMAGE}")
else
    images=("telemachy-ci:local" "localhost/hephaestus-ci:verify")
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
    echo "Build one with 'just ci-build' or set TELEMACHY_JUST_IMAGE." >&2
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
