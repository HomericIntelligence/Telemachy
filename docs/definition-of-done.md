# Definition of Done

This document captures what "done" means for a feature, bug-fix, or
documentation change in Telemachy. It complements `CONTRIBUTING.md`.

## Done — feature

- [ ] Pydantic models / workflow schema updated (if applicable).
- [ ] `pytest` covers each new conditional branch (positive + negative).
- [ ] `just lint` and `just format-check` clean.
- [ ] Public API change documented in `README.md` and `AGENTS.md`.
- [ ] If workflow YAML schema changes: bump `apiVersion` per
      `docs/backwards-compat.md` and update workflow examples.
- [ ] `CHANGELOG.md` `Unreleased` entry added (one bullet, user-facing language).
- [ ] PR description links the issue with `Closes #N`.
- [ ] At least one approving review (per CODEOWNERS / branch protection).
- [ ] CI green (required checks listed in `.github/`).

## Done — bug fix

- [ ] Regression test added that fails before the fix and passes after.
- [ ] `CHANGELOG.md` `Unreleased` entry under `### Fixed`.
- [ ] Root cause documented in the PR description.
- [ ] If the bug was a contract drift with Agamemnon, an ADR or note is
      added under `docs/`.

## Done — documentation

- [ ] Markdown lint passes (`just lint` / pre-commit `markdownlint`).
- [ ] Links resolve (relative paths checked locally).
- [ ] If the doc changes a policy (release, backwards compat, retention),
      reference it from `CONTRIBUTING.md`.

## Done — CI / tooling

- [ ] Workflow runs successfully on the PR.
- [ ] Required check list in branch protection updated if a job was
      renamed/added.
- [ ] Required-check workflow changes pass `tests/test_merge_queue.py` and
      preserve the live required-check policy and verify actual queue-head
      execution as described in
      [`docs/ci/merge-queue.md`](ci/merge-queue.md).
- [ ] Time and resource impact noted in the PR description.

## Not done

A PR that fails any of the above is *Not Done*. Reviewers should request
changes rather than merge with TODOs.
