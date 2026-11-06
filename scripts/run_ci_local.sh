#!/bin/bash
# Local CI entry point. Every subset executes inside the fleet CI image that
# AchaeanFleet builds, verifies, and publishes; the image selection, pull
# credential, mounts, and budgets live in scripts/container_run.sh.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUBSET="${1:-all}"

if [ "${SUBSET}" = "pull" ]; then
    exec "${SCRIPT_DIR}/container_run.sh" --pull
fi

exec "${SCRIPT_DIR}/container_run.sh" python3 ci/checks.py "${SUBSET}"
