# Merge queue verification

Telemachy's `main` branch already uses a merge queue. Its live repository rules
remain authoritative. This runbook verifies the current policy and actual queue
checks; it does not activate the queue or write branch protection.

PR #312 removed the full workflow's queue trigger in anticipation of a later
smoke-only protection policy. That policy change was not applied. Restoring the
real required checks preserves the current protection. The separate
`merge-queue-smoke` workflow is advisory and cannot satisfy the required suite.

## Current policy and check mapping

[`configs/github/merge-queue-policy.json`](../../configs/github/merge-queue-policy.json)
records the 13 required contexts and queue settings observed on 2026-09-12.
Re-read live protection before each acceptance run; stop on any mismatch.

| Required context | Job in `_required.yml` | Gate |
| --- | --- | --- |
| `build` | `build` | Build the Python distributions |
| `deps/version-sync` | `deps-version-sync` | Verify all version declarations |
| `install` | `install` | Install the wheel in an isolated environment |
| `integration-tests` | `integration-tests` | Run integration tests |
| `lint` | `lint` | Run language and configuration lint |
| `package` | `package` | Validate distribution metadata |
| `release` | `release` | Check version and changelog; no publication |
| `schema-validation` | `schema-validation` | Validate configuration syntax |
| `security/dependency-scan` | `security-dependency-scan` | Audit provisioned dependencies |
| `security/sast-scan` | `security-sast-scan` | Run the configured Bandit policy |
| `security/secrets-scan` | `security-secrets-scan` | Run blocking, redacted Gitleaks |
| `test` | `test` | Require both unit and integration jobs |
| `unit-tests` | `unit-tests` | Run unit tests and coverage |

The required workflow handles `push` to main, pull requests to main, and
`merge_group` `checks_requested`. Every context runs its real gate on the queue
commit. The tag-only `.github/workflows/release.yml` publisher remains separate.
The shared implementations and local equivalents are described in
[local CI](local-ci.md).

The live queue uses squash and `HEADGREEN`, with at most two entries building
concurrently and up to five entries merging together. It can form a group with
one entry after five minutes; required checks have 60 minutes to report.
`HEADGREEN` evaluates the group head's required checks. This does not reduce or
replace the 13 required contexts. Each CI container defaults to two CPUs, 4 GiB
of memory and 512 processes; workflow jobs retain their bounded timeouts.

## Verify a designated queued change

1. Complete independent review and actual local and hosted CI for the exact
   pull-request head before normal queue admission. Do not use an admin bypass.
2. Capture the live rules and compare them with the checked-in snapshot below.
3. Let the authorized coordinator admit the designated pull request through the
   normal expected-head queue path. The commands below observe an existing
   entry; they do not enqueue or merge it.
4. Bind the selected run to that entry's enqueue time and queue-head SHA. Wait
   for all 13 actual contexts, not only the fast advisory smoke job.
5. Preserve the raw rules, queue entry, workflow run and job/check responses.
   Record failure or non-completion honestly; a configuration test cannot prove
   that GitHub scheduled or passed a real queue run.

```bash
REPO=HomericIntelligence/Telemachy
POLICY=configs/github/merge-queue-policy.json
RULES_JSON="$(mktemp)"
gh api "repos/${REPO}/rules/branches/main" > "${RULES_JSON}"

jq --slurpfile policy "${POLICY}" -e '
  ([.[] | select(.type == "required_status_checks")
    | .parameters.required_status_checks[].context] | sort)
  == ($policy[0].required_contexts | sort)
  and
  ([.[] | select(.type == "merge_queue") | {type, parameters}]
    == [$policy[0].merge_queue_rule])
' "${RULES_JSON}"
# Keep RULES_JSON with the acceptance evidence; no policy mutation is performed.

SMOKE_PR=123  # Replace with the designated, already queued pull request.
OWNER="${REPO%%/*}"
NAME="${REPO#*/}"
REQUESTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
PR_HEAD_SHA="$(gh pr view "${SMOKE_PR}" --repo "${REPO}" \
  --json headRefOid --jq '.headRefOid')"
test -n "${PR_HEAD_SHA}"
printf 'smoke_pr=%s requested_at=%s pr_head_sha=%s\n' \
  "${SMOKE_PR}" "${REQUESTED_AT}" "${PR_HEAD_SHA}"

# Poll the designated PR until GitHub records its actual queue entry. The entry
# provides both the authoritative enqueue time and that entry's queue head SHA.
QUEUE_ENTRY=""
for attempt in {1..60}; do
  QUEUE_ENTRY="$(gh api graphql \
    -f owner="${OWNER}" -f name="${NAME}" -F number="${SMOKE_PR}" \
    -f query='
      query($owner: String!, $name: String!, $number: Int!) {
        repository(owner: $owner, name: $name) {
          pullRequest(number: $number) {
            mergeQueueEntry {
              enqueuedAt
              headCommit { oid }
            }
          }
        }
      }
    ' --jq '
      .data.repository.pullRequest.mergeQueueEntry
      | select(. != null and .headCommit != null)
      | {enqueuedAt, queueHeadSha: .headCommit.oid}
    ')"
  [[ -n "${QUEUE_ENTRY}" ]] && break
  sleep 10
done
test -n "${QUEUE_ENTRY}"

ENQUEUED_AT="$(jq -r '.enqueuedAt' <<<"${QUEUE_ENTRY}")"
QUEUE_HEAD_SHA="$(jq -r '.queueHeadSha' <<<"${QUEUE_ENTRY}")"
CURRENT_PR_HEAD_SHA="$(gh pr view "${SMOKE_PR}" --repo "${REPO}" \
  --json headRefOid --jq '.headRefOid')"
[[ "${CURRENT_PR_HEAD_SHA}" == "${PR_HEAD_SHA}" ]]
printf 'smoke_pr=%s enqueued_at=%s pr_head_sha=%s queue_head_sha=%s\n' \
  "${SMOKE_PR}" "${ENQUEUED_AT}" "${PR_HEAD_SHA}" "${QUEUE_HEAD_SHA}"

# Select only a newly-created merge_group run for this exact queue head. Do not
# use the repository's latest merge-group run: it may belong to a different PR.
RUN_JSON=""
for attempt in {1..60}; do
  RUN_JSON="$(gh api --method GET \
    "repos/${REPO}/actions/workflows/_required.yml/runs" \
    -f event=merge_group \
    -f head_sha="${QUEUE_HEAD_SHA}" \
    -f created=">=${ENQUEUED_AT}" \
    -f per_page=100 \
    | jq -c --arg queue_head_sha "${QUEUE_HEAD_SHA}" \
      --arg enqueued_at "${ENQUEUED_AT}" '
        [.workflow_runs[]
         | select(
             .event == "merge_group"
             and .head_sha == $queue_head_sha
             and .created_at >= $enqueued_at
           )]
        | sort_by(.created_at)
        | first // empty
      ')"
  [[ -n "${RUN_JSON}" ]] && break
  sleep 10
done
test -n "${RUN_JSON}"

RUN_ID="$(jq -r '.id' <<<"${RUN_JSON}")"
gh run watch "${RUN_ID}" --repo "${REPO}" --exit-status

RUN_JSON="$(gh api "repos/${REPO}/actions/runs/${RUN_ID}")"
jq -e --arg queue_head_sha "${QUEUE_HEAD_SHA}" \
  --arg enqueued_at "${ENQUEUED_AT}" '
    .event == "merge_group"
    and .head_sha == $queue_head_sha
    and .created_at >= $enqueued_at
    and .status == "completed"
    and .conclusion == "success"
  ' <<<"${RUN_JSON}"
jq '{id,event,head_sha,created_at,status,conclusion,html_url}' \
  <<<"${RUN_JSON}"

EXPECTED="$(jq -c '.required_contexts | sort' "${POLICY}")"
JOBS_JSON="$(mktemp)"
CHECK_RUNS_JSON="$(mktemp)"
trap 'rm -f "${JOBS_JSON}" "${CHECK_RUNS_JSON}"' EXIT

gh api --paginate \
  "repos/${REPO}/actions/runs/${RUN_ID}/jobs?per_page=100" \
  --jq '.jobs[]' | jq -s '.' > "${JOBS_JSON}"

JOB_NAMES="$(jq -c --argjson expected "${EXPECTED}" '
  [.[]
   | select(.name as $name | $expected | index($name))
   | .name]
  | sort
' "${JOBS_JSON}")"
[[ "${JOB_NAMES}" == "${EXPECTED}" ]]

jq -e --argjson expected "${EXPECTED}" \
  --arg queue_head_sha "${QUEUE_HEAD_SHA}" '
    [.[] | select(.name as $name | $expected | index($name))]
    | all(.[];
        .head_sha == $queue_head_sha
        and .status == "completed"
        and .conclusion == "success"
      )
  ' "${JOBS_JSON}"

while IFS= read -r check_run_url; do
  gh api "repos/${REPO}/check-runs/${check_run_url##*/}"
done < <(jq -r --argjson expected "${EXPECTED}" '
  .[]
  | select(.name as $name | $expected | index($name))
  | .check_run_url
' "${JOBS_JSON}") | jq -s '.' > "${CHECK_RUNS_JSON}"

CHECK_RUN_NAMES="$(jq -c '[.[].name] | sort' "${CHECK_RUNS_JSON}")"
[[ "${CHECK_RUN_NAMES}" == "${EXPECTED}" ]]

jq -e --arg queue_head_sha "${QUEUE_HEAD_SHA}" '
  all(.[];
    .head_sha == $queue_head_sha
    and .status == "completed"
    and .conclusion == "success"
  )
' "${CHECK_RUNS_JSON}"
```

The selected run is tied to the designated smoke pull request through its merge
queue entry, actual enqueue time, and exact queue head SHA. The job query then
follows that run's `check_run_url` values. Both the required job names and their
check-run names must equal the 13-context artifact exactly, without missing or
duplicate required contexts, and every required job and check run must finish
successfully. Additional advisory jobs may run but are not policy contexts.

Do not remove or rename any required context to make a queued change pass. The queue rule
does not carry a separate check list; it relies on the existing
`required_status_checks` rule in the same ruleset.

## A queued check is missing or fails

Stop acceptance and inspect the exact queue-head run and live rules. A source
repair can require a fresh queue entry after review and CI, which the authorized
coordinator manages separately. Preserve the failed entry and run evidence.
Do not rewrite check results, remove a required context, or use the historical
smoke-only policy proposal as a workaround. Any later protection change requires
its own explicit review and authorization.
