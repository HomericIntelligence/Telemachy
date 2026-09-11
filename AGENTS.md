# AGENTS.md — Telemachy

This document specifies the multi-agent coordination protocols for
Telemachy within the HomericIntelligence distributed agent mesh. It is
the sole authoritative agent contract for this repository: it also
covers single-agent operational conventions, development guidelines,
and guardrails (formerly in `CLAUDE.md`).

## Role

Telemachy is the declarative workflow engine: it parses YAML
workflows, provisions agents and teams via ProjectAgamemnon's REST API,
assigns dependency-ordered tasks, monitors completion, and tears down
resources.

```
Workflow YAML
    │
    ▼
WorkflowSpec ──► WorkflowExecutor ──► ProjectAgamemnon REST API
                                     ▲
                                     └─ provisions agents/teams/tasks
```

Telemachy is the **only** agent authorised to translate declarative
workflow YAML into Agamemnon API calls. Other agents must not bypass
Telemachy to provision Agamemnon resources unless explicitly handing
off (see "Handoff" below).

## Role boundaries

| Agent | Owns | Must not do |
| --- | --- | --- |
| Telemachy | workflow YAML interpretation, dependency ordering, teardown policy | spawn agents directly (always via Agamemnon) |
| ProjectAgamemnon | agent / team / task lifecycle, HMAS orchestration | parse workflow YAML |
| ProjectNestor | research and ideation upstream | execute workflows |
| ProjectArgus | observability, log aggregation | mutate state in Agamemnon |
| ProjectHermes | NATS event bridge | own agent lifecycle |

## Handoff contract

- **Inbound:** users (or ProjectNestor) deliver workflow YAML through the
  Typer CLI (`just run <file>`) or via library use of
  `telemachy.executor.WorkflowExecutor`.
- **Outbound:** Telemachy calls Agamemnon REST endpoints in the order
  documented in the "Architecture" section below. The handoff payload to
  Agamemnon is the AgentSpec / TeamSpec / TaskSpec record validated by
  `telemachy.models`.
- **Fleet registration:** `just fleet-register` / `register-fleet-epic` and
  `telemachy.fleet_registration.register_fleet_epic` accept a reviewed workflow,
  an existing canonical epic, and its explicit registration marker. They persist
  child creation and publication progress in that epic, then send the frozen
  `hi/v1` envelope to `hi.pipeline.epic.{epic_key}.registered` through Keystone's
  JetStream transport. An actual PubAck is required. Agamemnon's durable intake
  owns graph creation, dispatch, replay, and parent wakeups. The registration
  block never owns task decisions or Hephaestus stage labels.
  See [the Fleet registration contract](docs/fleet-registration.md) for retry,
  exact-byte integration, configuration, and evidence limits.

## Inter-agent message contracts

- All Agamemnon HTTP calls use `httpx.AsyncClient`; payloads conform to
  ProjectAgamemnon's published OpenAPI shape.
- Fleet registration publishes through the existing `homeric-pipeline` stream;
  it validates retention and never provisions or changes a stream. This is
  separate from task-completion monitoring.
- Future NATS-based completion monitoring will subscribe to subjects
  documented in Odysseus `docs/adr/005-nats-subject-schema.md`. Until
  that lands Telemachy polls Agamemnon via HTTP.

## Coordination invariants

1. **Agamemnon exclusive.** Telemachy never spawns processes,
   containers, or agents itself.
2. **Dependency-respecting.** Tasks with `blocked_by` are not submitted
   until predecessors complete.
3. **Idempotent teardown.** Re-running teardown for an already-torn-down
   workflow is a no-op.
4. **Pure planning.** `just plan` and `just validate` never mutate
   Agamemnon state.
5. **Fleet durable registration.** A confirmed GitHub intent precedes each
   child-create attempt and publication. Uncertain creates require exact-marker
   reconciliation; no timeout authorizes another create. Retries retain the
   frozen workflow and wire bytes. The explicit `--exclusive-writer` flag asserts
   an externally enforced deployment condition; it is not a distributed lock.
   First-epic creation and an acquired exclusive writer remain separate gates.

## Project Overview

Telemachy is a declarative workflow engine that automates multi-agent workflows by calling the
ProjectAgamemnon REST API. Users define workflows in YAML; Telemachy parses them, provisions agents and
teams via Agamemnon, assigns tasks with dependency ordering, monitors execution by polling
Agamemnon's REST API (NATS-based event monitoring is planned but not yet wired up), and
tears down resources according to the workflow's teardown policy.

**This project uses ProjectAgamemnon exclusively as its execution backend.**
There is no parallel agent system — all agent lifecycle management flows through Agamemnon's REST API.

## Architecture

```
Workflow YAML
    │
    ▼
WorkflowSpec (Pydantic)
    │
    ▼
WorkflowExecutor
    ├── AgamemnonClient  →  POST /v1/agents              (create agents)
    ├── AgamemnonClient  →  POST /v1/agents/{id}/start   (start agents)
    ├── AgamemnonClient  →  POST /v1/teams               (create teams)
    ├── AgamemnonClient  →  POST /v1/teams/{id}/tasks    (create tasks)
    └── AgamemnonClient  →  DELETE /v1/agents/{id}       (teardown)
```

_Planned (issue #92): a NATS subscriber consuming Agamemnon task-lifecycle events will
replace the HTTP polling loop in `_monitor_completion`. Not yet implemented._

## Implementation Status

✅ Implemented

- HTTP polling for task completion — `WorkflowExecutor._monitor_completion`
  (`src/telemachy/executor.py:311`), bounded by `settings.monitor_timeout_seconds`
  and `settings.monitor_max_polls`.
- HTTP polling for `blocked_by` dependency unblock inside
  `_assign_tasks` (`src/telemachy/executor.py` ~283-292).
- TLS scheme validation on `AGAMEMNON_URL` and `NATS_URL` in
  `AgamemnonClient.__init__` when `REQUIRE_TLS=true`
  (`src/telemachy/agamemnon_client.py:49-61`).

📋 Planned (tracked under #92)

- NATS subscriber consuming Agamemnon task-lifecycle events.
- Replacement of `_monitor_completion`'s polling loop with event-driven
  completion detection.

### Key Components

- `telemachy/models.py` — Pydantic models for the workflow schema (AgentSpec, TaskSpec, TeamSpec, WorkflowSpec, WorkflowState)
- `telemachy/agamemnon_client.py` — Async HTTP client wrapping all ProjectAgamemnon REST endpoints used
- `telemachy/rate_limiter.py` — Async token-bucket rate limiter for throttling outbound HTTP calls (#160)
- `telemachy/executor.py` — Orchestrates the full workflow lifecycle: provision → assign tasks → monitor → teardown
- `telemachy/cli.py` — Typer CLI (`run`, `plan`, `status`, `validate`, `list`, `cancel`)
- `telemachy/fleet_registration.py`, `fleet_github.py`, `fleet_publish.py` —
  issue-backed Fleet registration, confirmed GitHub mutations, JetStream PubAck
- `telemachy/workflow_input.py` — shared file validation/loading for CLI commands
- `telemachy/config.py` — Settings loaded from environment / `.env`
- `telemachy/telemetry.py` — Observability primitives (correlation IDs, structured logging, metrics, tracing)
- `docs/ROADMAP.md` — canonical roadmap for outstanding work (NATS
  subscriber under #92, state backend under v1.0.0). See ADR-003.

## Workflow Schema

```yaml
apiVersion: telemachy/v1
metadata:
  name: string
  description: string
agents:
  - name: string
    program: string          # default: claude-code
    model: string | null
    working_dir: string      # default: /tmp
    runtime: local | docker  # default: local
    docker_image: string | null
    cpus: int                # default: 2
    memory: string           # default: 4g
teams:
  - name: string
    agents: [string]         # references to agent names
    tasks:
      - subject: string
        description: string
        assign_to: string    # agent name
        blocked_by: [string] # task subjects
teardown: on_completion | on_failure | never
```

## Key Principles

The coordination invariants above (Agamemnon exclusive,
dependency-respecting, idempotent teardown) also apply as engineering
principles; note additionally that teardown errors are logged but do
not block. Beyond those:

1. **Declarative** — workflows describe desired state; Telemachy handles how to get there.
2. **Observable** — all state transitions are logged; completion is currently detected by HTTP
   polling against Agamemnon (NATS event-driven completion is planned).
   - **Correlation IDs**: Every log record carries a per-execution `workflow_id` for end-to-end tracing.
   - **Structured logging**: Logs can be emitted as plain text or JSON via `LOG_FORMAT` setting.
   - **Prometheus metrics**: Workflow completion, task outcomes, and HTTP latency are exposed when `METRICS_ENABLED=true`.
   - **OpenTelemetry tracing**: Spans are emitted for each workflow phase (provisioning, team creation, monitoring,
     teardown) when `OTEL_ENABLED=true`.
3. **Type-safe** — all Python code uses type hints; Pydantic validates all external data.

## Repository Structure

```
Telemachy/
├── src/
│   └── telemachy/
│       ├── __init__.py           # version
│       ├── cli.py                # Typer CLI entry point
│       ├── config.py             # Settings / env vars
│       ├── executor.py           # WorkflowExecutor
│       ├── agamemnon_client.py   # ProjectAgamemnon REST client
│       └── models.py             # Pydantic workflow models
├── workflows/
│   ├── example.yaml              # Simple 2-agent example
│   └── fleet-deploy.yaml         # Docker fleet example
├── tests/
│   ├── __init__.py
│   ├── test_models.py
│   └── test_executor.py
├── .env.example
├── AGENTS.md
├── README.md
├── justfile
└── pixi.toml
```

## Planned Features

Outstanding work is tracked in [`docs/ROADMAP.md`](docs/ROADMAP.md):

- **NATS subscriber** (#92) — event-driven task-completion monitoring,
  superseding HTTP polling.
- **Persistent state backend** — enables the `status`, `list`, and `cancel`
  CLI commands.

For the full planning picture — including release-line bucketing and
known limitations — see `docs/ROADMAP.md`. Roadmap drift is regression-
tested (`tests/test_roadmap.py`).

## Development Guidelines

- All Python files must have type hints on all functions and class attributes.
- Use `async`/`await` throughout for I/O operations (HTTP today; NATS once the subscriber lands under #92).
- Use `httpx.AsyncClient` for all HTTP calls; never `requests`.
- Pydantic v2 models for all structured data.
- Errors from Agamemnon should raise typed exceptions, not generic ones.
- Tests are split into **unit** tests (mock `AgamemnonClient`) and **integration** tests (drive a real
  `httpx.AsyncClient` through `tests/stub_agamemnon.py`, an in-process ASGI stub). Mark new lifecycle/end-to-end tests
  with `@pytest.mark.integration`. `just test` runs the full suite (unit + integration); `just test-unit` skips
  integration for fast iteration; `just test-integration` runs only the lifecycle suite. The stub returns HTTP 501 (not
  404) for any endpoint it does not implement so that a new Agamemnon endpoint surfaces as a named test failure.
  Integration tests construct stub-bound clients through `make_client_for(stub)` and register them with the
  `client_pool` fixture — never inline `httpx.AsyncClient(...)` in a test.
- CI enforces a `--cov-fail-under=75` coverage floor (sourced from `pyproject.toml` `[tool.coverage.report]`). Local
  `just test` does not pass `--cov` by default — reproduce the CI check with `pixi run pytest --cov=telemachy
  --cov-report=term-missing`.
- All `src/` Python must pass `pixi run python -m bandit -ll --ini .bandit`. Suppress
  findings inline with `# nosec <ID>  # <one-line rationale>`, not by widening
  the `.bandit` `skips` list. When adding a new package under `src/<name>/`,
  also update the `files:` regex in `.pre-commit-config.yaml` and the `-r`
  target in the `bandit` task in `pixi.toml`.

## Agent Guardrails

AI agents (Claude, Myrmidon swarm, automation) working in this repository must
obey the following hard rules. These guardrails apply on top of the rest of
this document, the project's `CONTRIBUTING.md`, and the user's per-machine
memory.

- **Never commit directly to `main`.** All changes flow through pull requests.
- **Never `--admin` merge.** Squash-merge through GitHub UI or `gh pr merge --auto --squash` only.
- **Never delete branches with `git branch -d/-D`.** Use a different branch name instead.
- **Never delete a running agent or team via Agamemnon.** Always follow the
  workflow's declared `teardown` policy (`on_completion`, `on_failure`,
  `never`) — never short-circuit it from an agent shell.
- **Always `just plan` before `just run`** on a workflow you didn't author.
- **Always `just validate` before opening a PR** that touches a workflow YAML.
- **Never bypass `pre-commit`** unless the brief explicitly authorises
  `--no-verify` (it stalls on cold worktrees; document the bypass in the
  PR body).
- **Never write to `pixi.lock` by hand;** regenerate via `pixi install`.
- **Always re-run the license audit when adding or major-bumping a
  runtime dependency.** Update `docs/license-audit.md` in the same
  PR; re-run `pixi run license-audit` to print the current declared
  set for cross-checking.
- **Workflow YAML is a public API** — any change to required fields,
  default values, or schema constraints requires a `MINOR` (additive) or
  `MAJOR` (breaking) version bump per `docs/backwards-compat.md`.
- **Never paste PII (names, emails, account IDs, secrets) into workflow
  `subject` or `description` fields.** `subject` is logged by Telemachy at
  INFO and both are transmitted to Agamemnon. See `docs/privacy.md`.

If a task appears to require violating one of the rules above, stop and
open an issue describing the conflict before proceeding.

## Common Commands

```bash
just run workflows/example.yaml    # execute a workflow
just plan workflows/example.yaml   # dry-run: print what would be created
just validate workflows/example.yaml  # validate YAML schema only
just test                          # run pytest
just lint                          # ruff check
just format                        # ruff format
just bandit                        # SAST scan (medium+ severity) on src/telemachy
just check                         # lint + mypy + bandit + test
just fleet-register reviewed.yaml --repo OWNER/REPO --epic-issue 42 \
  --registration-key reviewed-idea --dry-run  # offline Fleet registration preview
```

## Workflow State Persistence

`status`, `list`, and `cancel` are backed by a file-based `WorkflowState`
store (`src/telemachy/state_store.py`). `run` persists each workflow's
state as JSON under `TELEMACHY_STATE_DIR` (default `~/.telemachy/state`);
`status`/`list` read it back, and `cancel` writes a `<id>.cancel` sentinel
that the executor's watcher polls. This is independent of the planned NATS
event ingestion (#92), which scopes live task-lifecycle events only.

Implementation detail: cancellation propagates via the `<id>.cancel`
sentinel polled by `cli.run` (1s interval); the executor's existing
`stop_event` is set when the sentinel appears.

## Testing layers

- `just test` — full suite (unit + integration); this is what CI runs
- `just test-unit` — unit tests only; mocks `AgamemnonClient` at the method level
- `just test-integration` — integration tests under `tests/integration/`; exercises `WorkflowExecutor` against an
  in-process mock Agamemnon HTTP server (`respx`)

Integration tests must declare `pytestmark = [pytest.mark.integration, pytest.mark.asyncio]` at the top of each module.

## CI Triage

See `docs/ci-triage.md` when a CI job fails at "Set up job" or any
pre-`checkout` step. These are almost always GitHub-managed runner
infrastructure issues, not code defects, and have a documented triage
path that covers permissions, runner labels, and secrets references.

## Environment Variables

| Variable | Default | Description |
| --- | --- | --- |
| `AGAMEMNON_URL` | `http://localhost:8080` | ProjectAgamemnon base URL |
| `AGAMEMNON_API_KEY` | `` | API key (if auth enabled) |
| `AGAMEMNON_RATE_LIMIT_RPS` | `0` | Token-bucket refill rate (requests/sec) for outbound Agamemnon calls. `0` disables throttling. |
| `AGAMEMNON_RATE_LIMIT_BURST` | `16` | Maximum burst size for the token bucket. Must be `>= 1`; `0` or negative is rejected at startup. |
| `NATS_URL` | `nats://localhost:4222` | Broker URL for explicit Fleet registration and legacy registration; also validated by `AgamemnonClient` when `REQUIRE_TLS=true`. Task-completion subscription remains planned (#92). |
| `NATS_CLIENT_TOKEN` | (unset) | Optional backend token for the registration publisher; never persisted in issue metadata. |
| `GITHUB_TOKEN` | (unset) | Backend GitHub authorization for explicit Fleet registration; required for mutations and unused by offline preview. |
| `WORKFLOWS_DIR` | `workflows` | Directory to search for workflow YAML files |
| `HOST_ID` | `hermes` | Host identifier embedded in Agamemnon task assignments |
| `REQUIRE_TLS` | `true` | Reject non-TLS Agamemnon connections. Set to `false` to allow cleartext for local dev (logs a WARNING). |
| `LOG_LEVEL` | `INFO` | Python logging level (DEBUG, INFO, WARNING, ERROR) |
| `MONITOR_TIMEOUT_SECONDS` | `3600` | Seconds before workflow monitor times out |
| `MONITOR_MAX_POLLS` | `7200` | Maximum polling attempts for workflow monitor |
| `DEFAULT_WORKFLOW_TIMEOUT` | `7200` | Per-workflow execution timeout in seconds |
| `HEALTHCHECK_INTERVAL_SECONDS` | `15` | Seconds between Agamemnon liveness probes during `_monitor_completion`. |
| `HEALTHCHECK_FAILURE_THRESHOLD` | `2` | Consecutive failed probes before raising `WorkflowConnectivityError`. |
| `HEALTHCHECK_TIMEOUT_SECONDS` | `5` | Per-probe HTTP timeout (overrides client-wide 30s). |
| `TELEMACHY_STATE_DIR` | `~/.telemachy/state` | Directory for persisted `WorkflowState` JSON files and `<id>.cancel` sentinels. Read by `status`, `list`, `cancel`. `STATE_DIR` is also accepted as an alias. |
| `AUDIT_LOG_PATH` | (unset) | Path to JSONL audit log. When unset, audit emission is a no-op (NullSink). |
| `AUDIT_HASH_CHAIN` | `true` | SHA-256 hash chain for tamper-evident audit log. Resumes from existing file on restart. Disable only for tooling/tests. |
| `LOG_FORMAT` | `plain` | Log format: `plain` (human-readable, default) or `json` (one JSON object per line) |
| `METRICS_ENABLED` | `false` | Enable Prometheus metrics endpoint when `true` |
| `METRICS_PORT` | `9464` | Port to expose Prometheus metrics on when `METRICS_ENABLED=true` |
| `OTEL_ENABLED` | `false` | Enable OpenTelemetry tracing when `true` (console exporter only in this release) |
| `OTEL_SERVICE_NAME` | `telemachy` | Service name for OpenTelemetry resource |
| `OTEL_EXPORTER` | `console` | OTel exporter type (only `console` supported in this release; OTLP is a planned follow-up) |

## See also

- `CONTRIBUTING.md` — contribution workflow.
- Odysseus `docs/adr/005-nats-subject-schema.md`.

<!-- triage: 2026-04-24 myrmidon-swarm implementation pass complete -->
<!-- 17 PRs merged, all remaining issues tracked individually -->
