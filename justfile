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
