# === Variables ===

AGAMEMNON_URL := env_var_or_default("AGAMEMNON_URL", "http://localhost:8080")
NATS_URL      := env_var_or_default("NATS_URL", "nats://localhost:4222")

# === Default ===

default:
    @just --list

# === Workflow Execution ===

# Execute a workflow YAML file
run WORKFLOW:
    AGAMEMNON_URL={{AGAMEMNON_URL}} NATS_URL={{NATS_URL}} \
        pixi run python -m telemachy.cli run "{{WORKFLOW}}"

# Dry-run: show what would be created without executing
plan WORKFLOW:
    AGAMEMNON_URL={{AGAMEMNON_URL}} NATS_URL={{NATS_URL}} \
        pixi run python -m telemachy.cli plan "{{WORKFLOW}}"

# Validate a workflow YAML without executing
validate WORKFLOW:
    pixi run python -m telemachy.cli validate "{{WORKFLOW}}"

# Run the read-only MCP server over stdio (for local smoke testing)
mcp:
    AGAMEMNON_URL={{AGAMEMNON_URL}} NATS_URL={{NATS_URL}} \
        pixi run telemachy-mcp

# Export workflow JSON Schema for editor validation
schema:
    pixi run python -m telemachy.cli schema

# === Development ===

# Run the full test suite (unit + integration). Lifecycle tests run by default
# to satisfy issue #146; use `just test-unit` to skip them during fast iteration.
test:
    pixi run pytest

# Run only unit tests (everything outside tests/integration/)
test-unit:
    pixi run pytest -m "not integration" tests

# Run only integration tests (mock-Agamemnon HTTP layer)
test-integration:
    pixi run pytest -m integration tests/integration

# Run ruff linter
lint:
    pixi run ruff check src tests

# Run mypy static type checker
mypy:
    pixi run mypy src/telemachy --ignore-missing-imports

# Run Bandit SAST scan (medium+ severity)
bandit:
    pixi run python -m bandit -ll --ini .bandit -r src/telemachy

# Format code with ruff
format:
    pixi run ruff format src tests

# Run the full local CI suite: lint, mypy, bandit, tests
check: lint mypy bandit test

# Focused checks in an explicitly provisioned environment; never runs a solver.
fleet-registration-test python='python3':
    {{python}} -m pytest tests/test_github_epic.py tests/test_fleet_registration.py tests/test_fleet_github.py tests/test_fleet_cli.py -q

fleet-registration-integration python='python3' nats_server='nats-server':
    TELEMACHY_TEST_NATS_SERVER='{{nats_server}}' {{python}} -m pytest tests/integration/test_fleet_registration_transport.py -q

# Explicit durable registration; legacy register-epic remains unchanged.
[positional-arguments]
fleet-register *args:
    pixi run python -m telemachy.cli register-fleet-epic "$@"

# Install dev dependencies and set up pre-commit hooks
bootstrap:
    pixi install
    pixi run pre-commit install

# Cross-repository proof using existing binaries; never contacts live GitHub.
fleet-native-contract native broker output python='python3':
    PYTHONPATH="src:.${PYTHONPATH:+:$PYTHONPATH}" {{python}} tests/contracts/native_consumer.py '{{native}}' '{{broker}}' '{{output}}'

# === Containerized CI (podman by default) ===

# Build the CI container image (podman first, docker fallback)
ci-build:
    podman build --ignorefile ci/.dockerignore -f ci/Containerfile -t telemachy-ci:local . || docker build -f ci/Containerfile -t telemachy-ci:local .

# Run CI lint checks in container
ci-lint:
    ./scripts/run_ci_local.sh lint

# Run CI markdownlint checks in container
ci-markdownlint:
    ./scripts/run_ci_local.sh markdownlint

# Run CI pixi-check checks in container
ci-pixi-check:
    ./scripts/run_ci_local.sh pixi-check

# Run CI unit-tests checks in container
ci-unit-tests:
    ./scripts/run_ci_local.sh unit-tests

# Run CI integration-tests checks in container
ci-integration-tests:
    ./scripts/run_ci_local.sh integration-tests

# Run CI schema-validation checks in container
ci-schema-validation:
    ./scripts/run_ci_local.sh schema-validation

# Run CI security-secrets-scan checks in container
ci-security-secrets-scan:
    ./scripts/run_ci_local.sh security-secrets-scan

# Run CI deps-version-sync checks in container
ci-deps-version-sync:
    ./scripts/run_ci_local.sh deps-version-sync

# Run CI forbid-suppressions checks in container
ci-forbid-suppressions:
    ./scripts/run_ci_local.sh forbid-suppressions

# Run CI justfile-check checks in container
ci-justfile-check:
    ./scripts/run_ci_local.sh justfile-check

# Run CI symlink-check checks in container
ci-symlink-check:
    ./scripts/run_ci_local.sh symlink-check

# Run all CI checks in container
ci-all:
    ./scripts/run_ci_local.sh all
