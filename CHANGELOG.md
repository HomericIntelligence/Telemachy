# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

While the version remains `0.y.z`, the workflow YAML schema may receive
breaking changes in any `MINOR` bump — pin a specific `MINOR` until v1.0.0.
See `docs/backwards-compat.md`.

## [Unreleased]

### Added

- Provision the pinned, checksum-verified JetStream test broker in the existing
  Linux CI image so private-broker tests can run on clean CI hosts (Refs #338).

- Reject Fleet streams that can evict prior work through per-subject limits;
  document the public Fleet handoff and share CLI workflow input without a
  cyclic dependency (Refs #338).

- Add an explicit actual-producer-to-native-consumer contract with private
  JetStream and controlled GitHub failure/replay checks (Refs #338).

- Explicit `register-fleet-epic` API/CLI for a reviewed existing epic, with
  issue-backed child creation intents, frozen workflow identity, and a
  JetStream-acknowledged publication outbox. See `docs/fleet-registration.md`
  for the required single-writer deployment and unresolved first-epic intake gate
  (Refs #338).
- Release workflow (`.github/workflows/release.yml`) that builds
  sdist+wheel on `v*.*.*` tag pushes, cross-checks the tag against
  `pyproject.toml` and `__init__.py`, attaches artifacts to a GitHub
  Release with changelog-derived notes, and publishes to PyPI via
  Trusted Publishing. Closes #153, #176; part of #92.
- `docs/ROADMAP.md`, ADR-003, and `v0.1.0`/`v0.2.0`/`v1.0.0` GitHub
  milestones establishing the canonical planning artefact for
  Telemachy. Regression-tested by `tests/test_roadmap.py`.
  Closes #167; part of #92.
- WorkflowExecutor: declarative YAML-driven multi-agent orchestration with
  dependency-respecting task assignment, HTTP polling for completion, and
  teardown policies (`on_completion` / `on_failure` / `never`).
- AgamemnonClient: async `httpx` HTTP client wrapping all ProjectAgamemnon
  REST endpoints (agents, teams, tasks) with retry/backoff for transient
  failures (#23) and defensive response parsing (#24).
- CLI (`telemachy.cli`): Typer-based `run`, `plan`, `validate`, and
  `schema` commands; rich progress display for workflow execution (#62);
  workflow path validated against shell metacharacters (#43).
- Pydantic v2 workflow schema with `extra="forbid"` on every model to
  catch YAML typos (#51); `apiVersion` validator (#14); required
  `metadata.name` (#19); duplicate-subject detection within a team (#21);
  cross-team `assign_to` validation (#20).
- Configuration: `WorkflowExecutor` dry-run mode + `--dry-run` CLI flag
  (#58); event hooks for workflow lifecycle (#57); workflow-level
  execution timeout (#56); structured logging with `LOG_LEVEL` (#46);
  graceful shutdown on SIGINT/SIGTERM (#36); concurrent team provisioning
  via `asyncio.gather` (#55); workflows-directory setting (#15);
  configurable Docker `hostId` via `HOST_ID` env var (#11).
- JSON Schema export: `telemachy schema` CLI subcommand and
  `schemas/workflow-v1.json` generator for editor-side YAML validation
  (#52); justfile `schema` recipe.
- Release pipeline (`.github/workflows/release.yml`): tag-push trigger on
  `v*.*.*`, cross-checks the tag against `pyproject.toml` and
  `src/telemachy/__init__.py`, builds sdist+wheel, attaches them to a
  GitHub Release with notes sliced from this CHANGELOG, and publishes to
  PyPI via Trusted Publishing (#153).
- Packaging: `__all__` exports in `telemachy.__init__` for a clean public
  API (#53); PEP 561 `py.typed` marker (#45).
- Read-only MCP server (`telemachy-mcp`) exposing `agamemnon_list_agents`
  and `agamemnon_list_team_tasks` so AI agents can query Agamemnon state
  during development. Closes #173.

### Changed

- pixi: expand `platforms` to `linux-64`, `osx-arm64`, `osx-64`, `win-64`
  so contributors and operators on macOS and Windows can run
  `pixi install` (#180).
- Settings: `client_kwargs()` helper deduplicates `AgamemnonClient`
  construction across CLI/executor call sites (#231).
- Executor: tasks whose dependencies have failed are now skipped instead
  of blocking forever (#13).
- Executor: `_monitor_completion` bounded by `monitor_timeout_seconds`
  and `monitor_max_polls` (#7).
- Executor: teardown deletes teams in addition to agents (#5).
- Client: agent name resolved to ID before setting `assigneeAgentId`
  (#12).

### Changed (breaking)

- `REQUIRE_TLS` env var now defaults to `true` (was `false`). Closes #158;
  part of #92. `AgamemnonClient.__init__` raises `AgamemnonError` for
  plain `http://` Agamemnon URLs or plain `nats://` NATS URLs unless the
  operator explicitly sets `REQUIRE_TLS=false`. Local dev must opt back
  in; a `WARNING` is emitted at `Settings` construction.
- CLI: `status`, `list`, and `cancel` subcommands removed (#42) — they
  were non-functional stubs. A persistent workflow-state backend is
  required before they can be reintroduced; until then, query
  ProjectAgamemnon directly.

### Fixed

- Client: kwargs typing tightened for `AgamemnonClient` construction so
  mypy catches mismatches (#236).
- Executor: `backlog` removed from `_DONE_STATUSES` — it is an initial
  state, not terminal (#2).
- Models: workflow schema aligned with the real ProjectAgamemnon API
  field names (`subject`, `blockedBy`, `agentIds`, status enum).
- CLI: field references corrected from legacy `title`/`depends_on` to
  `subject`/`blocked_by`.
- Config: migrated to `pydantic-settings`; `pixi.toml` workspace name
  standardised.
- Pixi: `run` task forwards arguments so `WORKFLOW` is required (#29).

### Security

- TLS scheme validation on `AGAMEMNON_URL` and `NATS_URL` when
  `REQUIRE_TLS=true` (#34) — see also the breaking-change entry above.
- Workflow path sanitised against shell metacharacters in the CLI (#43).
- `gitleaks` secrets-scanning job added to CI (#35).
- Pre-commit pre-push hook requires every pushed commit to be GPG/SSH
  signed (#242).

### Removed

- `MaestroError` / `MaestroClient` backward-compat aliases removed; the
  client is ProjectAgamemnon-only following ADR-006.

### Documentation

- License audit: `docs/license-audit.md` records the BSD-3-Clause-only
  dependency set and is wired into the contributor flow (#262).
- README/CLAUDE.md/CONTRIBUTING.md realigned to reflect that NATS
  monitoring is planned-only and HTTP polling is the current backend
  (#245); env-var table completed in CLAUDE.md (#232); all 10 settings
  documented in `.env.example` (#230).
- Project-policy docs added: `LICENSE`, `SECURITY.md`, `CONTRIBUTING.md`,
  `CODE_OF_CONDUCT.md`, `docs/RELEASING.md`, `docs/backwards-compat.md`,
  `docs/definition-of-done.md`, `docs/install.md`.
- GitHub issue templates and PR template added (#37).
- README schema-field correction: `title` → `subject`,
  `depends_on` → `blocked_by` (#17).
- CODE_OF_CONDUCT: enforcement email replaced with a working address
  (#228).

### Build & CI

- Coverage gate: `--cov-fail-under=75` enforced on the `Test (pytest)`
  step (#152); coverage reporting added (#10).
- All GitHub Actions pinned by 40-char commit SHA (#49); unified
  `_required.yml` workflow defines the required-checks contract.
- Typecheck steps merged into the lint job (#90).
- `yamllint` step + config (#59); `markdownlint` action and
  MD060/MD013/MD022/MD032/MD009 corrections (#237, #227).
- Pre-commit `forbid-or-true` and `forbid-continue-on-error` hooks
  prevent silent-failure idioms in CI/shell/justfile sources (#239).
- Pixi: `pixi.lock` excluded from VCS; `.gitignore` consolidated
  (#22, #28, #30).
- Ruff lint+format configuration in `pyproject.toml` (#50);
  `.editorconfig` and pre-commit bootstrap recipe (#38).
- Dependabot bumps: `setup-pixi` 0.8.1 → 0.9.5 → 0.9.6;
  `actions/checkout` v4.2.2 → v6.0.2; `extractions/setup-just`
  2.0.0 → 4.0.0; `markdownlint-cli2-action`.

### Tests

- `AgamemnonClient` HTTP-interaction tests (#9).
- CLI tests for `validate`, `plan`, `run` (#8).
- Executor error-path tests for failed deps, timeout, teardown, and
  partial failures (#224).
