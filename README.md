# Telemachy

[![CI](https://github.com/HomericIntelligence/Telemachy/actions/workflows/ci.yml/badge.svg)](https://github.com/HomericIntelligence/Telemachy/actions/workflows/ci.yml)

A declarative workflow engine for [ProjectAgamemnon](https://github.com/HomericIntelligence/ProjectAgamemnon).
Define multi-agent workflows in YAML — Telemachy handles provisioning, task orchestration, completion monitoring,
and teardown via the Agamemnon REST API.

## Overview

Telemachy reads a workflow YAML file, creates agents and teams through ProjectAgamemnon,
assigns tasks with dependency ordering, monitors task completion by polling the Agamemnon
REST API (event-driven NATS monitoring is planned — see issue #92), and tears down
provisioned resources according to your policy.

No separate agent system is used. All execution is driven exclusively through ProjectAgamemnon.

## MCP Server

See [docs/mcp.md](docs/mcp.md) for the read-only MCP server that lets AI agents query live Agamemnon state during development.

## Quick Start

```bash
# Copy and configure environment
cp .env.example .env
# Edit .env with your AGAMEMNON_URL and API key

# Run a workflow
just run workflows/example.yaml

# Dry-run: see what would be created
just plan workflows/example.yaml

# Validate a workflow file
just validate workflows/example.yaml
```

> **Note:** `status`, `list`, and `cancel` subcommands are not yet
> implemented — persistent workflow-state storage is out of scope for
> the current release. Until a state backend lands, query
> ProjectAgamemnon directly for live agent and team status.

## Roadmap

See [`docs/ROADMAP.md`](docs/ROADMAP.md) for the canonical roadmap —
current, next, and future releases plus known limitations. Outstanding
work (NATS subscriber, state backend for `status`/`list`/`cancel`) is
tracked under epic [#92](https://github.com/HomericIntelligence/Telemachy/issues/92).

## Workflow Schema

A workflow YAML has four top-level sections:

```yaml
apiVersion: telemachy/v1

metadata:
  name: my-workflow
  description: "What this workflow does"

agents:
  - name: researcher
    program: claude-code
    model: claude-opus-4-5
    working_dir: /workspace
    runtime: local          # local | docker

  - name: coder
    program: claude-code
    runtime: docker
    docker_image: ghcr.io/homericintelligence/achaeanfleet:latest
    cpus: 4
    memory: 8g

teams:
  - name: core-team
    agents:
      - researcher
      - coder
    tasks:
      - subject: "Research the domain"
        description: "Investigate the problem space and summarize findings."
        assign_to: researcher

      - subject: "Implement solution"
        description: "Based on research findings, implement the solution."
        assign_to: coder
        blocked_by:
          - "Research the domain"

teardown: on_completion   # on_completion | on_failure | never
```

### Agent Fields

| Field | Default | Description |
| --- | --- | --- |
| `name` | required | Unique name within workflow |
| `program` | `claude-code` | Agent program to run |
| `model` | null | Override model (e.g. `claude-opus-4-5`) |
| `working_dir` | `/tmp` | Agent working directory |
| `runtime` | `local` | `local` or `docker` |
| `docker_image` | null | Required when `runtime: docker` |
| `cpus` | `2` | CPU allocation (docker only) |
| `memory` | `4g` | Memory allocation (docker only) |

### Teardown Policies

- `on_completion` — delete all agents and teams when all tasks succeed
- `on_failure` — delete only on failure (preserve state on success for inspection)
- `never` — never auto-teardown; manual cleanup required

## Idempotency

For the explicit Fleet issue-registration path, see
[Durable Fleet epic registration](docs/fleet-registration.md). It uses an
existing reviewed epic, issue-backed creation intents, and JetStream PubAck;
its retry contract is separate from workflow resource provisioning below.

Telemachy tags every agent and team it creates with a `tlm-<workflow>-<resource>` key stored in
Agamemnon. When a workflow is re-run (e.g. after a partial failure), resources whose keys already
exist in Agamemnon are reused rather than re-created, making retries safe by default.

**Team membership caveat:** When a team is reused from a prior run, its member list is trusted
verbatim from Agamemnon and is not reconciled against the current workflow spec. If a partial prior
run left a team with stale agent IDs (e.g. because an agent was re-created under a new ID), the
reused team will still point at the old agent ID and the freshly-created member will not be wired
in. Use `--force` to force creation of fresh resources and avoid this inconsistency:

```bash
just run workflows/example.yaml -- --force
```

## Privacy & PII

Task `subject` and `description` are forwarded to ProjectAgamemnon, and
`subject` appears in Telemachy logs at INFO. See
[`docs/privacy.md`](docs/privacy.md) before putting personal data into a
workflow YAML.

## Development

```bash
just test      # run tests
just lint      # ruff check
just format    # ruff format
```
