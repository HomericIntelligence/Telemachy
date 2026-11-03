# Installing and Upgrading Telemachy

Telemachy is a Python package distributed primarily for the
HomericIntelligence ecosystem. This document explains how to install it
outside the pinned `pixi` development environment.

## Supported installation modes

### 1. `pixi` (development; recommended)

```bash
git clone https://github.com/HomericIntelligence/Telemachy.git
cd Telemachy
pixi install
pixi run just test
```

This is the only installation path covered by CI and the test suite.
The `pixi` solve covers `linux-64`, `osx-arm64`, `osx-64`, and `win-64`;
all four are exercised in CI.

### 2. `pip install --editable .` (contributor outside pixi)

```bash
python -m venv .venv && source .venv/bin/activate
pip install --upgrade pip
pip install -e .[dev]
```

Use this when you cannot install `pixi` (e.g., on a restricted host).
You are responsible for matching the Python and dependency versions
declared in `pyproject.toml`; CI does not run this path.

### 3. `pip install` from a release tag (operator)

```bash
pip install "git+https://github.com/HomericIntelligence/Telemachy@vX.Y.Z"
```

This installs the `telemachy` CLI and library into the active Python
environment. Pin the tag exactly — `main` may carry unreleased schema
changes.

### 4. As a library dependency (downstream Python project)

Add to your `pyproject.toml`:

```toml
[project]
dependencies = [
    "telemachy @ git+https://github.com/HomericIntelligence/Telemachy@vX.Y.Z",
]
```

Pin to a specific tag, not a branch.

## Upgrading

1. Read `CHANGELOG.md` between your current version and the target
   version, paying attention to the `### Changed`, `### Removed`, and
   `### Deprecated` sections.
2. Read `docs/backwards-compat.md` to determine whether the upgrade
   crosses a breaking-change boundary.
3. If crossing a breaking boundary, follow the migration steps in the
   relevant CHANGELOG entry.
4. Upgrade in a non-production environment first; run
   `just validate workflows/<your>.yaml` to confirm the workflow still
   parses.

## Uninstalling

```bash
pip uninstall telemachy            # if installed via pip
# or remove the pixi-managed checkout directory
rm -rf <repo-checkout>/.pixi
```

Workflow definitions and live task state live in ProjectAgamemnon.
Telemachy additionally persists its own `WorkflowState` JSON under
`TELEMACHY_STATE_DIR` (default `~/.telemachy/state`) for `status`,
`list`, and `cancel`; remove that directory to reset local workflow
history.

## Troubleshooting

- **`No module named telemachy`** — confirm the active environment
  matches the installation mode above.
- **CLI commands hang** — confirm `AGAMEMNON_URL` is reachable; see the
  env vars table in `AGENTS.md`.
- **`Workflow schema validation failed`** — run `just validate` to get
  the structured error, and consult `docs/backwards-compat.md` if the
  workflow worked on a previous version.
