# Reproduce CI locally

`ci/checks.py` is the canonical check implementation. Local and hosted runs
execute it inside the Telemachy CI image, which
[AchaeanFleet](https://github.com/HomericIntelligence/AchaeanFleet) builds,
verifies, and publishes. The image recipe lives in AchaeanFleet at
[`ci/telemachy/`](https://github.com/HomericIntelligence/AchaeanFleet/tree/main/ci/telemachy);
this repository consumes the published artifact and does not build it.

`scripts/container_run.sh` is the only entry point that starts a container. The
required checks, the release workflow, the local suite, and every `just` recipe
call it, so one image, one credential, and one set of mounts serve all of them.

## Run the suite

1. Install Podman or Docker. The image already carries Pixi, Just, Gitleaks,
   NATS Server, Markdownlint CLI2, and npm, so no other host tooling is needed.
2. Export a pull credential once, then run `just ci-all`: see
   [Pull credentials](#pull-credentials). The runner pulls
   `ghcr.io/homericintelligence/achaean-telemachy-ci:ci` on first use, then
   uses two CPUs, 4 GiB of memory, and a 512-process ceiling by default.
   `TELEMACHY_CI_CPUS` and `TELEMACHY_CI_MEMORY` set explicit operator budgets.
   Network access is required for locked dependencies and advisory lookups.
   Missing tools, unavailable advisory services, findings, and failed commands
   fail the applicable check.
3. Inspect each printed subset and its diagnostics. The suite stops at the first
   failure. After a fix, rerun its subset with
   `bash scripts/run_ci_local.sh <subset>`, then the full suite with
   `just ci-all`. Subsets that consume distributions are ordered by the suite:
   `build` produces them, and `package`/`install` consume them.
4. Run `just ci-runner-test` for fast regression tests of the runner. These tests
   use private copied repositories and controlled external commands. They do
   not execute an engine or replace the actual Linux suite.

## Where the image is stored

| Field | Value |
| --- | --- |
| Registry | `ghcr.io/homericintelligence` |
| Image | `achaean-telemachy-ci` |
| Consumed tag | `:ci` (alias refreshed on each publish) |
| Published tags | `:ci`, `:latest`, `:git-<sha>`, `:<YYYY-MM-DD>` |
| Publisher | AchaeanFleet `.github/workflows/telemachy-ci.yml` |
| Recipe | AchaeanFleet `ci/telemachy/Containerfile` |

AchaeanFleet publishes on every push to its `main` that changes
`ci/telemachy/**`, and on manual dispatch. Toolchain changes belong in that
recipe; nothing in this repository builds the image. A local build tagged
`telemachy-ci:local` takes precedence and is the way to exercise a recipe
change before publishing:

```bash
podman build -f /path/to/AchaeanFleet/ci/telemachy/Containerfile \
  -t telemachy-ci:local /path/to/AchaeanFleet/ci/telemachy
bash scripts/run_ci_local.sh lint
```

`TELEMACHY_CI_IMAGE` overrides the reference entirely, for example to test a
`git-<sha>` tag before it reaches `:ci`.

### Pull credentials

The package is private. Organization policy forbids public container
packages, so no anonymous client can read it and no workflow or operator can
change that. CI owns access instead:

- Hosted required jobs read the repository `GHCR_TOKEN` secret, declared once
  at workflow level in `.github/workflows/_required.yml`.
- AchaeanFleet stores the same credential as `TELEMACHY_GHCR_TOKEN` and fails
  its publish unless that credential resolves `:ci` and `:git-<sha>`.
- Locally, export a classic personal access token with `read:packages`:

```bash
export GHCR_TOKEN=<token>
```

`GHCR_ACTOR` overrides the registry username, which defaults to `git`. A pull
without a credential fails before any check runs and names the missing
variable. Fork pull requests receive no secrets, so their jobs cannot pull the
image; run the suite from a branch in this repository or locally instead.

## One entry point for commands

`scripts/container_run.sh` resolves the image, authenticates the pull, mounts
the checkout, and runs one command inside it. Recipes in the justfile call it
directly, so `just test`, `just lint`, and `just validate` use the same
toolchain as the required checks:

```bash
just test                       # pytest inside the CI image
just validate workflows/example.yaml
bash scripts/container_run.sh pixi run --locked ruff check src tests
```

The runner passes the documented settings (`AGAMEMNON_URL`, `NATS_URL`,
`GITHUB_TOKEN`, `TELEMACHY_STATE_DIR`, and the rest of the table in the
repository guide) into the container by name, so a value never appears in the
process list. Workflow commands request the host network because Agamemnon and
NATS listen on the host loopback; check commands stay isolated.

`TELEMACHY_HOST=1` runs the same command on the host instead. Use it on macOS
or Windows hosts without a container engine, offline, or when debugging a
command that only reproduces outside the image:

```bash
TELEMACHY_HOST=1 just test
```

## Host `just` older than the justfile

`justfile-check` evaluates the task file with the image's pinned Just. A host
whose `just` predates the `[positional-arguments]` attribute (older than 1.31)
cannot parse the justfile at all, so neither `just --evaluate` nor any recipe
runs. Use the container copy instead of upgrading the host binary:

```bash
bash scripts/just_container.sh             # same as --evaluate
bash scripts/just_container.sh --list
```

The helper probes `telemachy-ci:local` first and then the published
`achaean-telemachy-ci:ci` image, pulling the latter when it is absent, and
accepts the first candidate whose `just --version` is at least 1.31. It never
builds an image. `TELEMACHY_JUST_IMAGE` selects an explicit image, and
`CONTAINER_ENGINE` selects Podman or Docker, as in `scripts/run_ci_local.sh`.

## Shared checks

Every hosted job runs its gate through `scripts/run_ci_local.sh`, as the local
recipes do, including `forbid-suppressions`, `security/sast-scan`, `build`,
`package`, `install`, and `release`. No job installs a toolchain on the runner.
The exception is the library steps that only a GitHub Action can perform:
artifact upload and download, SARIF upload, release creation, and the PyPI
Trusted Publishing action. The tag-only release workflow runs its version
cross-check and its build in the same image. Existing required check names,
merge-queue policy, and tag-only publishing are preserved.

| Subset | Enforced behavior |
| --- | --- |
| `forbid-suppressions` | Reject silent shell failures and workflow opt-outs in tracked source |
| `pixi-check`, `deps-version-sync` | Install with `--locked`; reject manifest/lock drift |
| `lint` | Ruff, mypy, and YAML validation of tracked first-party files |
| `markdownlint` | Validate tracked Markdown with the existing rule configuration |
| `justfile-check` | Evaluate the task file |
| `symlink-check` | Require every tracked symlink and its target to exist |
| `unit-tests` | Preserve full-suite coverage of at least 75% |
| `integration-tests` | Select integration tests, including the private JetStream broker |
| `merge-queue-smoke` | Validate workflow YAML against the vendored GitHub-workflows schema, lint it, run the queue contract tests, and check symlinks |
| `schema-validation` | Export the public workflow schema |
| `security/secrets-scan` | Run Gitleaks with repository configuration, redaction, and a SARIF receipt; retain its exit status |
| `security/dependency-scan` | Audit all external installed Python packages and the npm tool closure |
| `security/sast-scan` | Run the configured medium-and-higher Bandit checks |
| `build`, `package`, `install` | Build, validate metadata, install one wheel, and run isolated imports/CLI help |
| `release` | Check version agreement and a stageable changelog; never publish |

Security subset CLI names replace `/` with `-`, for example
`bash scripts/run_ci_local.sh security-dependency-scan`. Generated environments,
dependency trees, and private agent prompt files do not become first-party lint
targets. CLI2's native ignore configuration preserves the existing Markdown
exclusions.

## Tool and storage contracts

The recipe pins Python 3.13 and Node 26 image indexes by digest and verifies
full upstream archive SHA-256 values before installing Pixi, Just, Gitleaks, and
NATS Server for the target architecture. Those pins and the Markdownlint CLI2
npm closure live in AchaeanFleet under `ci/telemachy/`. Hex-level provenance for
every artifact stays inspectable there; no repository checkout is a runtime
dependency of the image.

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
Runtime requirements come from `pyproject.toml`, matching the wheel metadata
consumers receive; the smoke check does not add dependencies to wheel metadata.

Full CI evidence requires actual execution of the selected gates. Fixture
results, lock generation, or successful image construction alone do not prove
the full suite, live GitHub durability, agent admission, or Fleet performance.
