# Reproduce CI locally

1. Install Podman or Docker, then run `just ci-build` from the repository root.
   `CONTAINER_ENGINE` selects an explicit engine. A failed build returns failure;
   it does not retry against a different engine. The image supports native
   Linux amd64 and arm64. Existing Pixi platforms remain supported.
2. Run `just ci-all`. The runner uses two CPUs, 4 GiB of memory, and a 512-process
   ceiling by default. `TELEMACHY_CI_CPUS` and `TELEMACHY_CI_MEMORY` set explicit
   operator budgets. Network access is required for locked dependencies and
   advisory lookups. Missing tools, unavailable advisory services, findings,
   and failed commands fail the applicable check.
3. Inspect each printed subset and its diagnostics. The suite stops at the first
   failure. After a fix, run its individual `just ci-*` recipe, then the full
   suite. `just ci-package-build` creates the distributions consumed by
   `just ci-package` and `just ci-install`; the full suite orders these steps.
4. Run `just ci-runner-test` for fast regression tests of the runner. These tests
   use private copied repositories and controlled external commands. They do
   not execute an engine or replace the actual Linux suite.

## Shared checks

`ci/checks.py` is the canonical check implementation. Container-backed hosted
jobs use `scripts/run_ci_local.sh`, as the local recipes do. The hosted package,
SAST, release, and suppression jobs invoke the same check implementation with
the locked Pixi environment or standard-library Python, as applicable. Existing
required check names, merge-queue policy, and tag-only publishing are preserved.

| Subset | Enforced behavior |
| --- | --- |
| `forbid-suppressions` | Reject silent shell failures and workflow opt-outs in tracked source |
| `pixi-check`, `deps-version-sync` | Install with `--locked`; reject manifest/lock drift |
| `lint` | Ruff, mypy, and YAML validation of tracked first-party files |
| `markdownlint` | Validate tracked Markdown with the existing rule configuration |
| `justfile-check` | Evaluate the task file |
| `symlink-check` | Require every tracked symlink and its target to exist |
| `unit-tests` | Preserve full-suite coverage of at least 75% |
| `integration-tests` | Run the existing service-boundary integration selection |
| `schema-validation` | Export the public workflow schema |
| `security/secrets-scan` | Run Gitleaks with repository configuration, redaction, and a SARIF receipt; retain its exit status |
| `security/dependency-scan` | Audit all external installed Python packages and the npm tool closure |
| `security/sast-scan` | Run the configured medium-and-higher Bandit checks |
| `build`, `package`, `install` | Build, validate metadata, install one wheel, and run isolated imports/CLI help |
| `release` | Check version agreement and a stageable changelog; never publish |

Security subset CLI names replace `/` with `-`, for example
`just ci-security-dependency-scan`. Generated environments, dependency trees,
and private agent prompt files do not become first-party lint targets. CLI2's
native ignore configuration preserves the existing Markdown exclusions.

## Tool and storage contracts

The CI image pins Python 3.13 and Node 26 image indexes by digest. Its executable
manifest verifies full upstream archive SHA-256 values before installing Pixi
0.70.2, Just 1.51.0, Gitleaks 8.30.1, and NATS Server 2.10.24 for the target
architecture. NATS is only a private test broker. The hashes come from the
upstream release artifacts; see [the NATS checksum manifest](https://github.com/nats-io/nats-server/releases/download/v2.10.24/SHA256SUMS).

`ci/package-lock.json` pins Markdownlint CLI2 0.23.2 and its complete npm closure,
including the scoped `smol-toml` 1.7.1 fix. Its installer and initial archive
metadata were reused from AchaeanFleet's CI tooling and retained as local
provenance. Every artifact URL and digest remains inspectable in
`ci/tool-artifacts.json`; no repository checkout is a runtime dependency.

`pixi.lock` pins pip-audit 2.10.1, build 1.6.1, Hatchling 1.32.0, Twine 7.0.0,
and the remaining development dependencies. The audit freezes all installed
external packages, including packaging tools, into exact requirements. Only
the unpublished `telemachy` project is excluded. Strict pip-audit checks that
inventory without resolving a different environment; unsupported entries and
collection errors still fail. No advisory is ignored.

The runner stores Linux environments in `.ci-pixi/<image-architecture>` and
mounts them at `/workspace/.pixi`. An adjacent directory supplies a writable
CI home for the invoking user under either engine. It never deletes or reuses
the laptop's `.pixi` environment. Download caches live under `~/.cache/pixi`.
For a Git worktree, the runner also mounts its resolved common Git metadata
directory read-only at its original path so the existing `.git` pointer works.
It does not mount the parent checkout or export Git overrides into test-created
repositories. Both the Linux environment and generated reports are ignored by
Git. Package installation uses a fresh temporary virtual environment and
imports outside the source tree.
Runtime requirements come from `pixi.toml`, matching the supported installation
contract; the smoke check does not add dependencies to wheel metadata.

Full CI evidence requires actual execution of the selected gates. Fixture
results, lock generation, or successful image construction alone do not prove
the full suite, live GitHub durability, agent admission, or Fleet performance.
