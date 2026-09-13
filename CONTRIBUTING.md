# Contributing to Telemachy

Thank you for your interest in contributing to Telemachy! This is the workflow
execution engine for the [HomericIntelligence](https://github.com/HomericIntelligence)
distributed agent mesh — it runs, plans, monitors, and cancels named workflow YAML files
against Agamemnon's REST API. (A NATS-based event channel is planned — see issue #92 — but is not yet wired up.)

For an overview of the full ecosystem, see the
[Odysseus](https://github.com/HomericIntelligence/Odysseus) meta-repo.

## Quick Links

- [Development Setup](#development-setup)
- [What You Can Contribute](#what-you-can-contribute)
- [Development Workflow](#development-workflow)
- [Building and Testing](#building-and-testing)
- [Pull Request Process](#pull-request-process)
- [Code Review](#code-review)

## Where to find the roadmap

Planning lives in [`docs/ROADMAP.md`](docs/ROADMAP.md). Cross-cutting
work is bucketed into milestones (`v0.1.0`, `v0.2.0`, `v1.0.0`) and
tagged with the `roadmap`, `nats-subscriber`, or `state-backend`
labels. Open an issue with label `roadmap` to propose a change before
sending a PR that alters planned scope.

## Development Setup

### Prerequisites

- [Git](https://git-scm.com/)
- [GitHub CLI](https://cli.github.com/) (`gh`)
- [Pixi](https://pixi.sh/) for environment management (installs Python 3.13)
- [Just](https://just.systems/) as the command runner

### Environment Setup

```bash
# Clone the repository
git clone https://github.com/HomericIntelligence/Telemachy.git
cd Telemachy

# Activate the Pixi environment
pixi shell

# Copy and customize environment variables
cp .env.example .env

# List available recipes
just --list
```

### Verify Your Setup

```bash
# Run tests
just test

# Validate an existing workflow
just validate <WORKFLOW>
```

## What You Can Contribute

- **Workflow steps** — New step types for the execution engine (`src/telemachy/`)
- **Workflow YAML definitions** — Reusable workflows in `workflows/`
- **Validation logic** — Schema validation and input checking improvements
- **Tests** — pytest test cases for engine logic and step execution
- **Justfile recipes** — New workflow management commands
- **Documentation** — README updates, workflow authoring guides

### Workflow YAML Format

Workflow definitions live in the `workflows/` directory. Reference existing workflows as
examples for the expected schema. Workflows define steps that execute against the
Agamemnon REST API. Inter-agent NATS messaging is part of the planned event-monitoring
work (#92) and is not currently used by the engine.

## Development Workflow

### 1. Find or Create an Issue

Before starting work:

- Browse [existing issues](https://github.com/HomericIntelligence/Telemachy/issues)
- Comment on an issue to claim it before starting work
- Create a new issue if one doesn't exist for your contribution

### 2. Branch Naming Convention

Create a feature branch from `main`:

```bash
git checkout main
git pull origin main
git checkout -b <issue-number>-<short-description>

# Examples:
git checkout -b 10-add-parallel-step-type
git checkout -b 7-fix-workflow-cancellation
```

**Branch naming rules:**

- Start with the issue number
- Use lowercase letters and hyphens
- Keep descriptions short but descriptive

### 3. Commit Message Format

We follow [Conventional Commits](https://www.conventionalcommits.org/):

```text
<type>(<scope>): <subject>

<body>

<footer>
```

**Types:**

| Type       | Description                |
|------------|----------------------------|
| `feat`     | New feature                |
| `fix`      | Bug fix                    |
| `docs`     | Documentation only         |
| `style`    | Formatting, no code change |
| `refactor` | Code restructuring         |
| `test`     | Adding/updating tests      |
| `chore`    | Maintenance tasks          |

**Example:**

```bash
git commit -m "feat(engine): add parallel step execution

Implements a 'parallel' step type that runs multiple child steps
concurrently with configurable concurrency limits.

Closes #10"
```

## Building and Testing

### Test

```bash
# Run all tests (pytest)
just test
```

### Reproduce the required CI gates

Run `just ci-build`, then `just ci-all` for the bounded Linux container suite.
The local runner and hosted jobs use the same check implementations, including
Markdown, dependency audits, packaging, installed-wheel checks, and release
validation. The release check does not publish anything. See
[the CI runbook](docs/ci/local-ci.md) for tool pins, individual subsets, and
environment storage.

### Lint and Format

```bash
# Run linter (ruff)
just lint

# Auto-format
just format
```

### Run Workflows

```bash
# Run a named workflow
just run <WORKFLOW>

# Preview execution plan (dry run)
just plan <WORKFLOW>

# Validate workflow YAML
just validate <WORKFLOW>
```

> `status`, `list`, and `cancel` are not yet implemented — see the
> "Planned Features" section in [AGENTS.md](./AGENTS.md).

### Python Conventions

- **Python version**: 3.13 (managed by Pixi; versions below 3.14)
- **Project layout**: src layout (`src/telemachy/`)
- **Build backend**: hatchling (`pyproject.toml`)
- **Type hints**: Required for all function parameters and return types
- **Linting/formatting**: ruff

## Pull Request Process

### Before You Start

1. Ensure an issue exists for your work
2. Create a branch from `main` using the naming convention
3. Implement your changes
4. Run `just test` and `just lint` to verify

### Creating Your Pull Request

```bash
git push -u origin <branch-name>
gh pr create --title "[Type] Brief description" --body "Closes #<issue-number>"
```

**PR Requirements:**

- PR must be linked to a GitHub issue
- PR title should be clear and descriptive
- Tests and linting must pass

### Merge queue verification

Telemachy's `main` branch uses an active merge queue. Complete independent review
and current-head CI before normal queue admission. See
[`docs/ci/merge-queue.md`](docs/ci/merge-queue.md) to compare live protection with
the recorded policy and verify all 13 required checks on the actual queue head.
Confirm the queue entry rather than assuming that enabling auto-merge created
one. Missing checks require investigation and repair; do not weaken protection
or use an admin bypass.

### Changelog discipline

Any PR that changes user-visible behavior — CLI flags, workflow YAML
schema, env vars, public Python imports under `telemachy.*`, or runtime
defaults — must add a bullet to the appropriate section of
`CHANGELOG.md`'s `## [Unreleased]` block in the same PR. Group the entry
under `Added` / `Changed (breaking)` / `Changed` / `Fixed` / `Security` /
`Removed`, and reference the issue or PR number in parentheses.

Pure-internal changes (refactors with no API change, test-only changes,
CI workflow edits that don't change contributor commands, Dependabot
bumps for dev-only deps) do not require a CHANGELOG entry. When in
doubt, add one — entries are cheap; missed breaking changes are not.

The PR template's "Test plan" checklist includes a CHANGELOG line that
must be ticked (or explicitly waived in the PR description as
internal-only) before merge.

### Never Push Directly to Main

The `main` branch is protected. All changes must go through pull requests.

## Code Review

### What Reviewers Look For

- **Workflow safety** — Are step inputs validated and sanitized?
- **Error handling** — Are failures handled gracefully with proper cleanup?
- **Test coverage** — Are new step types and engine logic tested?
- **No hardcoded secrets** — Are credentials in environment variables?
- **YAML schemas** — Do new workflow definitions conform to the schema?

### Responding to Review Comments

- Keep responses short (1 line preferred)
- Start with "Fixed -" to indicate resolution

## Markdown Standards

All documentation files must follow these standards:

- Code blocks must have a language tag (`python`, `bash`, `yaml`, `text`, etc.)
- Code blocks must be surrounded by blank lines
- Lists must be surrounded by blank lines
- Headings must be surrounded by blank lines

## Reporting Issues

### Bug Reports

Include: clear title, steps to reproduce, expected vs actual behavior, workflow YAML if relevant.

### Security Issues

**Do not open public issues for security vulnerabilities.**
See [SECURITY.md](SECURITY.md) for the responsible disclosure process.

## Adding or upgrading a runtime dependency

1. Add the dependency to `pixi.toml` `[pypi-dependencies]` with a
   `>=<lower-bound>` constraint (never `*`).
2. Run `pixi install` to regenerate `pixi.lock`; commit both files.
3. Update `docs/license-audit.md` with the dependency's declared
   license and a yes/no compatibility decision against BSD-3-Clause.
4. Run `pixi run license-audit` and confirm the printed table
   matches the Findings section of `docs/license-audit.md`.

## Code of Conduct

Please review our [Code of Conduct](CODE_OF_CONDUCT.md) before contributing.

---

Thank you for contributing to Telemachy!
