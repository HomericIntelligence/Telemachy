# Backwards Compatibility Policy

This document defines what backwards compatibility means for
Telemachy and how breaking changes are handled.

## Public surface area (governed by SemVer)

The following surfaces follow [Semantic Versioning 2.0.0](https://semver.org/):

1. **Workflow YAML schema** — the structure validated by
   `telemachy.models.WorkflowSpec`:
   - `apiVersion`, `metadata`, `agents`, `teams`, `tasks`, `teardown` fields.
   - Required versus optional status of each field.
   - Default values applied when a field is omitted.
   - Allowed values for enumerated fields (e.g., `runtime`, `teardown`).
2. **Typer CLI** — the subcommand names, required arguments, and exit codes
   for `run`, `plan`, `status`, `validate`, `list`, `cancel`, `schema`, and
   `register-fleet-epic`.
3. **Python public API** — names exported from `telemachy/__init__.py` and
   `telemachy.executor`, `telemachy.models`, `telemachy.config`.
4. **Environment variables** — names and defaults documented in `AGENTS.md`.

## Not public

- Internal HTTP retry / poll tuning constants.
- Logging format and verbosity defaults.
- Test fixtures, internal `_`-prefixed helpers, anything inside
  `telemachy/_internal/` (if such a package is introduced).
- The exact wording of CLI output (only its structure and exit code).

## What counts as breaking

A change is **breaking** (requires `MAJOR` bump, or pre-1.0 `MINOR` bump) if it:

- Removes or renames a field on the workflow schema.
- Changes the default value of a field in a way that changes runtime behaviour.
- Tightens the allowed values of an enumerated field.
- Adds a new **required** field to the workflow schema.
- Renames or removes a CLI subcommand, required argument, or supported flag.
- Removes a symbol from the Python public API or changes its signature.
- Renames or removes a documented environment variable.

A change is **non-breaking** if it:

- Adds a new optional field to the workflow schema with a sensible default.
- Adds a new CLI subcommand or optional flag.
- Adds a new Python public symbol.
- Adds a new environment variable with a default.

## Deprecation policy

Before removing or renaming a public surface:

1. Mark the surface as deprecated in a `MINOR` (or `PATCH` if pre-1.0)
   release. Emit a `DeprecationWarning` for Python symbols; log a `WARNING`
   for CLI usage; for workflow schema, log a `WARNING` on `validate`.
2. Add a `### Deprecated` entry in `CHANGELOG.md` and document the
   replacement.
3. Keep the deprecated surface working for **at least one minor release**
   before removal.
4. Remove in the next `MAJOR` release (or pre-1.0 `MINOR` release).

## Migration guide template

When a breaking change ships, the `CHANGELOG.md` entry must include:

```markdown
### Changed (breaking)

- `<surface>` — what changed, why, and how to migrate.
  - Before: <example>
  - After:  <example>
```

A standalone migration document is added under `docs/migrations/X.Y.md`
when the migration is non-trivial (more than 5 lines of guidance).

## Pre-1.0 caveat

While the version remains `0.y.z`, the workflow schema may receive
breaking changes in any `MINOR` bump. Operators should pin a specific
`MINOR` until v1.0.0.
