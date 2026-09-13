"""Regression tests for GitHub merge-queue readiness."""

import json
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
REQUIRED_WORKFLOW = WORKFLOWS_DIR / "_required.yml"
RELEASE_WORKFLOW = WORKFLOWS_DIR / "release.yml"
SMOKE_WORKFLOW = WORKFLOWS_DIR / "merge-queue-smoke.yml"
MERGE_QUEUE_POLICY = REPO_ROOT / "configs" / "github" / "merge-queue-policy.json"
MERGE_QUEUE_RUNBOOK = REPO_ROOT / "docs" / "ci" / "merge-queue.md"

EXPECTED_REQUIRED_CONTEXTS = [
    "build",
    "deps/version-sync",
    "install",
    "integration-tests",
    "lint",
    "package",
    "release",
    "schema-validation",
    "security/dependency-scan",
    "security/sast-scan",
    "security/secrets-scan",
    "test",
    "unit-tests",
]

EXPECTED_MERGE_QUEUE_RULE = {
    "type": "merge_queue",
    "parameters": {
        "check_response_timeout_minutes": 60,
        "grouping_strategy": "HEADGREEN",
        "max_entries_to_build": 2,
        "max_entries_to_merge": 5,
        "merge_method": "SQUASH",
        "min_entries_to_merge": 1,
        "min_entries_to_merge_wait_minutes": 5,
    },
}


def _load_workflow(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text())
    assert isinstance(data, dict), f"{path} must contain a workflow mapping"
    return data


def _on_block(workflow: dict[str, Any]) -> dict[str, Any]:
    """Return the Actions trigger block despite PyYAML's YAML 1.1 `on` coercion."""
    on_block = workflow.get(True, workflow.get("on"))
    assert isinstance(on_block, dict), "workflow `on` block must be a mapping"
    return on_block


def _load_policy() -> dict[str, Any]:
    policy = json.loads(MERGE_QUEUE_POLICY.read_text())
    assert isinstance(policy, dict), "merge-queue policy must be a JSON object"
    return policy


def _job(workflow: dict[str, Any], job_id: str) -> dict[str, Any]:
    job = workflow["jobs"][job_id]
    assert isinstance(job, dict), f"workflow job {job_id!r} must be a mapping"
    return job


def _step(job: dict[str, Any], name: str) -> dict[str, Any]:
    step = next(item for item in job["steps"] if item.get("name") == name)
    assert isinstance(step, dict), f"workflow step {name!r} must be a mapping"
    return step


def test_policy_artifact_pins_exact_required_contexts() -> None:
    assert _load_policy()["required_contexts"] == EXPECTED_REQUIRED_CONTEXTS


def test_policy_artifact_pins_exact_approved_queue_rule() -> None:
    assert _load_policy()["merge_queue_rule"] == EXPECTED_MERGE_QUEUE_RULE


def test_required_workflow_push_main_block_is_exact() -> None:
    on_block = _on_block(_load_workflow(REQUIRED_WORKFLOW))
    assert on_block["push"] == {"branches": ["main"]}


def test_required_workflow_pull_request_main_block_is_exact() -> None:
    on_block = _on_block(_load_workflow(REQUIRED_WORKFLOW))
    assert on_block["pull_request"] == {"branches": ["main"]}


def test_required_workflow_accepts_queue_checks_without_path_filters() -> None:
    on_block = _on_block(_load_workflow(REQUIRED_WORKFLOW))
    assert on_block.get("merge_group") == {"types": ["checks_requested"]}


def test_queue_required_jobs_and_dependencies_have_no_event_exclusions() -> None:
    """Bind queue output names to real jobs, including aggregate dependencies.

    This checks the supported unconditional configuration, not a simulation of
    the Actions scheduler. A real merge-group run must still pass these checks.
    """
    jobs = _load_workflow(REQUIRED_WORKFLOW)["jobs"]
    pending = []
    for context in EXPECTED_REQUIRED_CONTEXTS:
        matching = [job_id for job_id, job in jobs.items() if job.get("name", job_id) == context]
        assert len(matching) == 1, (context, matching)
        pending.extend(matching)
    visited = set()
    while pending:
        job_id = pending.pop()
        if job_id in visited:
            continue
        visited.add(job_id)
        job = jobs[job_id]
        assert "if" not in job, (job_id, "required job must run for queue events")
        assert not job.get("continue-on-error"), job_id
        assert any(
            "run" in step and "if" not in step and not step.get("continue-on-error")
            for step in job["steps"]
        ), job_id
        needs = job.get("needs", [])
        pending.extend([needs] if isinstance(needs, str) else needs)


def test_smoke_workflow_handles_merge_group_checks_requested_only() -> None:
    on_block = _on_block(_load_workflow(SMOKE_WORKFLOW))
    assert on_block == {"merge_group": {"types": ["checks_requested"]}}


def test_smoke_workflow_runs_exactly_one_fast_smoke_job() -> None:
    jobs = _load_workflow(SMOKE_WORKFLOW)["jobs"]
    assert list(jobs) == ["merge-queue-smoke"]
    assert jobs["merge-queue-smoke"]["name"] == "merge-queue-smoke"
    assert jobs["merge-queue-smoke"]["timeout-minutes"] == 5


def test_required_workflow_emits_every_policy_context_exactly_once() -> None:
    jobs = _load_workflow(REQUIRED_WORKFLOW)["jobs"]
    emitted_names = [
        job.get("name", job_id) for job_id, job in jobs.items() if isinstance(job, dict)
    ]
    policy_contexts = _load_policy()["required_contexts"]

    emitted_policy_contexts = [name for name in emitted_names if name in policy_contexts]

    assert sorted(emitted_policy_contexts) == policy_contexts
    assert len(emitted_policy_contexts) == len(set(emitted_policy_contexts))


def test_required_gitleaks_scan_fails_on_detected_secrets() -> None:
    workflow = _load_workflow(REQUIRED_WORKFLOW)
    scan = _step(_job(workflow, "security-secrets-scan"), "Run Gitleaks")["run"]

    # The canonical runner's external invocation and failure behavior are
    # exercised by test_ci_runner; this binds the required context to it.
    assert scan.strip() == "bash scripts/run_ci_local.sh security-secrets-scan"


def test_required_gitleaks_sarif_upload_runs_after_scan_failure() -> None:
    workflow = _load_workflow(REQUIRED_WORKFLOW)
    upload = _step(_job(workflow, "security-secrets-scan"), "Upload Gitleaks SARIF")

    assert upload["if"] == "always() && hashFiles('gitleaks.sarif') != ''"


def test_smoke_runbook_correlates_new_run_to_smoke_pr_queue_head() -> None:
    runbook = MERGE_QUEUE_RUNBOOK.read_text()

    for marker in (
        "SMOKE_PR=",
        "PR_HEAD_SHA=",
        "ENQUEUED_AT=",
        "QUEUE_HEAD_SHA=",
        "event=merge_group",
        'head_sha="${QUEUE_HEAD_SHA}"',
        'created=">=${ENQUEUED_AT}"',
    ):
        assert marker in runbook
    assert "--limit 1" not in runbook


def test_smoke_runbook_verifies_selected_run_terminal_result() -> None:
    runbook = MERGE_QUEUE_RUNBOOK.read_text()

    for assertion in (
        '.event == "merge_group"',
        ".head_sha == $queue_head_sha",
        '.status == "completed"',
        '.conclusion == "success"',
    ):
        assert assertion in runbook


def test_smoke_runbook_compares_exact_job_and_check_run_names_to_policy() -> None:
    runbook = MERGE_QUEUE_RUNBOOK.read_text()

    assert 'EXPECTED="$(jq -c \'.required_contexts | sort\' "${POLICY}")"' in runbook
    assert "actions/runs/${RUN_ID}/jobs?per_page=100" in runbook
    assert ".check_run_url" in runbook
    assert 'CHECK_RUN_NAMES="' in runbook
    assert '[[ "${JOB_NAMES}" == "${EXPECTED}" ]]' in runbook
    assert '[[ "${CHECK_RUN_NAMES}" == "${EXPECTED}" ]]' in runbook
    assert "all(.;" not in runbook
    assert runbook.count("all(.[];") == 2
    assert "repos/${REPO}/check-runs/${check_run_url##*/}" in runbook


def test_release_publisher_remains_tag_only() -> None:
    on_block = _on_block(_load_workflow(RELEASE_WORKFLOW))

    assert on_block["push"] == {"tags": ["v*.*.*"]}
    assert "pull_request" not in on_block
    assert "merge_group" not in on_block
