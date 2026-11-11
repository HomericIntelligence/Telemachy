# CI Triage Guide

## "Set up job" and Other Pre-Checkout Failures

GitHub Actions "Set up job" and other pre-`checkout` failures run inside the GitHub-managed runner image
before any user step executes. This means **workflow edits cannot directly fix these failures** — the failure
happens before our code even checks out.

The only repo-side levers available to influence pre-checkout failures are:

1. **`permissions:` block** — controls GITHUB_TOKEN scopes (e.g., `contents: read`)
2. **`runs-on:` label** — specifies the runner (e.g., `ubuntu-latest`, `ubuntu-24.04`)
3. **`secrets.*` references** — must be valid, configured secrets in the repo

All other pre-job failures are GitHub Actions infrastructure issues: runner allocation, image provisioning,
secret access, or temporary service disruptions.

## Triage Checklist

When a CI job fails at "Set up job" or any pre-checkout step:

### 1. Pull the Logs

```bash
gh run view <RUN-ID> --log-failed --repo HomericIntelligence/Telemachy
```

Look for the specific error message. Common patterns:

- `unable to resolve action <action-ref>@<SHA>` — action ref or commit SHA doesn't exist
- `missing gitleaks license` — a required secret is not configured
- `Node.js 20 actions are deprecated` — action uses Node.js 20; will be forced to Node 24 soon
- Runner provisioning timeout — GitHub infrastructure issue, likely transient

### 2. Check GitHub Status

Visit <https://www.githubstatus.com/> and check if there is an active or recent incident affecting GitHub Actions
at the time of the failure. Note the incident ID if one matches the failure timestamp.

### 3. Re-run the Job

**If the run is recent (< 90 days old):**

```bash
gh run rerun <RUN-ID> --failed --repo HomericIntelligence/Telemachy
gh run watch <RUN-ID> --repo HomericIntelligence/Telemachy
```

Wait for the re-run to complete. If it passes, the failure was transient.

**If the run is stale (> 90 days old or `rerun` command fails):**

Trigger a fresh `workflow_dispatch` run on the same commit SHA:

```bash
gh workflow run ci.yml --ref <COMMIT-SHA> --repo HomericIntelligence/Telemachy
```

Then observe the new run. If it passes, the failure was transient.

### 4. Interpret the Result

- **Re-run passes** → failure was transient. File it as such. Link the
  GitHub status incident (if any) in a closing comment. Close the tracking
  issue.
- **Re-run fails on the same step and same SHA** → not transient; proceed
  to repo-side audit (next section).

## Repo-Side Audit (Three Checks)

If a failure recurs on the same commit SHA and is not covered by a GitHub status incident, audit the workflow
statically for repo-side issues.

### Check 1: Permissions

```bash
grep -nE "permissions:" .github/workflows/ci.yml
```

If a `permissions:` block is present, it must list **only** the token scopes actually used by the job:

```yaml
permissions:
  contents: read        # needed for: actions/checkout
  checks: write         # needed for: setting check run status
  # do not add 'pull-requests: write' if the job doesn't create/update PR comments
```

**What to look for:**

- Overly broad scopes (e.g., `pull-requests: write` when not needed)
- Missing scopes that a step actually requires (unlikely, but possible)

If the permissions block is missing and you're seeing auth-related failures, add one with the minimal required scopes.

### Check 2: Runner Labels

```bash
grep -nE "runs-on:" .github/workflows/ci.yml
```

Verify every `runs-on:` uses a standard GitHub-hosted label:

- `ubuntu-latest` (recommended)
- `ubuntu-24.04` or `ubuntu-22.04` (specific versions)
- A self-hosted label that you know is online and healthy

**What to look for:**

- Non-standard or misspelled labels
- Self-hosted labels that are offline
- Deprecated runner labels (e.g., `ubuntu-18.04`)

### Check 3: Secrets References

```bash
grep -oE '\$\{\{[[:space:]]*secrets\.[A-Z_]+[[:space:]]*\}\}' \
  .github/workflows/ci.yml \
  | sed 's/.*secrets\.//' \
  | sed 's/[[:space:]]*}}.*//' \
  | sort -u
```

Compare the referenced secret names against the configured secrets in the repo:

```bash
gh secret list --repo HomericIntelligence/Telemachy --json name --jq '.[].name' | sort -u
```

**What to look for:**

- Any secret referenced in the workflow but NOT in the `gh secret list`
  output — this is a defect.
- Example: if the workflow uses `${{ secrets.GITLEAKS_LICENSE }}` but
  `GITLEAKS_LICENSE` is not in the repo's secret list, you must either:
  - Add the secret to the repo via GitHub Settings (out-of-band)
  - Remove the reference from the workflow if it's not actually used

## Escalation Path

If all three repo-side audits (permissions, runner labels, secrets) pass, and the failure recurs on the same SHA, then
the issue is likely a GitHub Actions infrastructure problem or a runner-environment-specific issue. In that case:

1. Open a [GitHub Support ticket](https://github.com/contact/github-support) with:
   - The failing run URL
   - The specific error message from the logs
   - The SHA and workflow file name
2. Add the label `needs-github-support` to the tracking issue
3. Link the support ticket in a comment on the tracking issue

## Related Issues

- **Issue #219** — example case: CI workflow 'Set up job' failure, root cause was invalid pixi action SHA (since fixed).
  Fixed by PR #221 (commit `87dc30e`, "correct setup-pixi SHA pin to valid
  v0.8.1 commit") and subsequent dependency bumps.
- **Issue #151** — workflow analysis and GitHub reusable-workflow strategy (referenced for scope justification).

## Common Root Causes (by frequency)

1. **Invalid action SHA or version** (most common)
   - A pinned action reference points to a commit SHA or tag that no longer exists
   - Fix: update the action pin to a valid version, usually via Dependabot PR
   - Example: PR #235 bumped `prefix-dev/setup-pixi` to v0.9.5.

2. **Missing or misconfigured secret**
   - A step requires an environment variable or input that sources a secret, but the secret is not configured in the repo
   - Fix: add the secret via repo Settings, or remove the reference if the secret is not needed

3. **Deprecated or Node.js version incompatibility**
   - An action uses an unsupported Node.js version (e.g., Node 20 deprecated as of June 2, 2026)
   - Fix: update the action to a newer version that supports the current Node.js version
   - Usually handled by Dependabot PRs

4. **Runner allocation or GitHub Actions service disruption**
   - GitHub's runner infrastructure is temporarily unavailable or overloaded
   - Fix: wait and re-run; these are almost always transient
   - Check <https://www.githubstatus.com/> for ongoing incidents

5. **Over-broad or missing token permissions**
   - The `permissions:` block is missing or doesn't include a required scope
   - Fix: add the missing scope (e.g., `contents: read` for checkout, `checks: write` for check annotations)
