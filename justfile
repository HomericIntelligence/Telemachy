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

# Export workflow JSON Schema for editor validation
schema:
    pixi run python -m telemachy.cli schema

# === Development ===

# Run the full test suite (unit + integration).
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

# Evaluate the justfile with the CI image's just (host just may be too old).
justfile-check:
    ./scripts/just_container.sh --evaluate

# Focused checks in an explicitly provisioned environment; never runs a solver.
fleet-registration-test python='python3':
    {{python}} -m pytest tests/test_fleet_registration.py tests/test_fleet_github.py tests/test_fleet_cli.py -q

fleet-registration-integration python='python3' nats_server='nats-server':
    TELEMACHY_TEST_NATS_SERVER='{{nats_server}}' {{python}} -m pytest tests/integration/test_fleet_registration_transport.py -q

# Explicit durable registration.
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

# === Containerized CI (see docs/ci/local-ci.md) ===

# Pull the CI image AchaeanFleet publishes for Telemachy's checks.
ci-pull:
    ./scripts/run_ci_local.sh pull

# Run every CI check in the published CI image. Individual subsets:
#   ./scripts/run_ci_local.sh <subset>
ci-all:
    ./scripts/run_ci_local.sh all

# Fixture checks execute copied scripts with fake external tools; no engine.
ci-runner-test:
    pixi run --locked pytest tests/test_ci_runner.py --no-cov -q
