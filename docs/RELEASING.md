# Releasing Telemachy

This document describes the release process for Telemachy.
Most steps are automated by `.github/workflows/release.yml`.

## Version sources of truth

Versions appear in:

1. `pyproject.toml` — `version` (canonical).
2. `src/telemachy/__init__.py` — `__version__` constant.
3. `CHANGELOG.md` — `## [X.Y.Z] - YYYY-MM-DD` heading.

All three must agree at release time. The `deps-version-sync` job in
`.github/workflows/_required.yml` enforces this on every PR by parsing
`pyproject.toml` and `src/telemachy/__init__.py` with `ast.literal_eval`
and exiting non-zero on mismatch. The release workflow's
`verify-version` job (`.github/workflows/release.yml:16-62`) is the
second-layer check that the pushed tag agrees with both sources.

## Versioning policy

Telemachy follows [Semantic Versioning 2.0.0](https://semver.org/).
The public surface area governed by SemVer is:

- The **workflow YAML schema** (`apiVersion: telemachy/v1`, field names,
  field types, default values).
- The Typer CLI subcommand contract (`run`, `plan`, `validate`, `schema`,
  `register-fleet-epic`) — names and required arguments.
- Public Python imports under `telemachy.*` (executor, models, config).

Internal: HTTP client retry tuning, monitor poll interval defaults,
logging output, and test fixtures are **not** public API.

While the version remains `0.y.z` the workflow schema may receive
breaking changes in any `MINOR` bump; operators should pin a specific
`MINOR` until v1.0.0.

## Release procedure

1. Decide the bump kind (`MAJOR`, `MINOR`, `PATCH`).
2. Update the three version sources above in a single commit:
   `chore(release): bump version to X.Y.Z`.
3. Update `CHANGELOG.md`:
   - Move accumulated `## [Unreleased]` entries under
     `## [X.Y.Z] - YYYY-MM-DD`.
   - Add a fresh empty `## [Unreleased]` block above.
4. Open a PR titled `chore(release): X.Y.Z`.
5. After squash-merge, create an annotated git tag on the squash commit
   and push it:

   ```bash
   git tag -a vX.Y.Z -m "Release X.Y.Z"
   git push origin vX.Y.Z
   ```

6. The `.github/workflows/release.yml` workflow runs automatically on the
   tag push: it cross-checks the tag against `pyproject.toml` and
   `src/telemachy/__init__.py`, builds the sdist+wheel, creates a GitHub
   Release (body sliced from the matching `## [X.Y.Z]` block in
   `CHANGELOG.md`) with both artifacts attached, and publishes to PyPI
   via Trusted Publishing (OIDC).
7. If the release workflow fails, do **not** delete and re-push the tag.
   Land a fix on `main`, bump to the next `PATCH`, and tag that.
8. Announce the release in the Odysseus integration channel.

## Automation

- Workflow: `.github/workflows/release.yml` (triggered on `v*.*.*` tag push).
- PyPI binding: the `telemachy` project on PyPI must have a Trusted
  Publisher configured for `HomericIntelligence/Telemachy`,
  workflow file `release.yml`, environment `pypi`. Configure once at
  <https://pypi.org/manage/account/publishing/>. Until this is configured,
  the `pypi-publish` job will fail with an OIDC token-exchange error —
  which is the intended fail-loud behaviour, not a silent skip.
- All third-party actions in the workflow are pinned by 40-char commit
  SHA; bumping a pin requires re-resolving via
  `gh api repos/<owner>/<repo>/git/refs/tags/<tag>`.

## Who approves

A release PR requires at least one CODEOWNERS approval. The release
author is responsible for verifying CI green before tagging.

## Hot-fixes

For patch releases off a previous `MINOR`:

1. Branch from the last `vX.Y.0` tag as `release/X.Y`.
2. Cherry-pick (or re-author) the minimal fix; bump `PATCH`.
3. Tag `vX.Y.Z+1` from the release branch.
4. Forward-port the fix to `main` in a separate PR.
