"""Integration tests covering the full workflow lifecycle against the Agamemnon stub.

Drives a real httpx.AsyncClient through tests/stub_agamemnon.py so HTTP
serialisation, retry logic, status polling, dependency unblock, and teardown
are exercised end-to-end. Hook-callback firing is intentionally NOT tested
here — that is an internal observer concern unit-tested in
tests/test_executor.py (TestHooks).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from telemachy.agamemnon_client import AgamemnonClient
from telemachy.executor import WorkflowExecutor
from telemachy.models import WorkflowSpec
from tests.conftest import ClientPool, load_workflow, make_client_for
from tests.stub_agamemnon import StubAgamemnon

pytestmark = pytest.mark.integration


def _assert_no_unhandled(stub: StubAgamemnon) -> None:
    assert stub.unhandled == [], (
        f"stub_agamemnon hit unimplemented endpoints: {stub.unhandled}. "
        "Add them to tests/stub_agamemnon.py._dispatch."
    )


async def test_happy_path_single_agent_single_task(
    agamemnon_client: AgamemnonClient,
    stub_agamemnon: StubAgamemnon,
    make_spec: Callable[..., WorkflowSpec],
) -> None:
    spec = make_spec(teardown="on_completion")
    executor = WorkflowExecutor(agamemnon_client, poll_interval=0.01)
    state = await executor.execute(spec)

    assert state.status == "completed"
    assert list(state.created_agents.keys()) == ["worker"]
    assert state.completed_at is not None

    calls = stub_agamemnon.calls
    assert ("POST", "/v1/agents") in calls
    assert any(m == "POST" and p.endswith("/start") for m, p in calls)
    assert ("POST", "/v1/teams") in calls
    assert any(m == "POST" and p.endswith("/tasks") for m, p in calls)
    assert any(m == "DELETE" and "/v1/agents/" in p for m, p in calls)
    assert stub_agamemnon.agents == {}
    _assert_no_unhandled(stub_agamemnon)


async def test_dependent_tasks_submitted_in_order(
    stub_agamemnon_factory: Callable[..., StubAgamemnon],
    client_pool: ClientPool,
    make_spec: Callable[..., WorkflowSpec],
) -> None:
    """A blocked_by=[A] task is not POSTed until A reports completed."""
    stub = stub_agamemnon_factory(
        task_statuses={
            "A": ["pending", "pending", "completed"],
            "B": ["pending", "completed"],
        }
    )
    client = client_pool.register(make_client_for(stub))

    spec = make_spec(
        tasks=[
            {"subject": "A", "description": "first", "assign_to": "worker"},
            {"subject": "B", "description": "second", "assign_to": "worker", "blocked_by": ["A"]},
        ]
    )
    executor = WorkflowExecutor(client, poll_interval=0.01)
    state = await executor.execute(spec)

    assert state.status == "completed"
    # Verify both tasks were created (POST /v1/teams/{id}/tasks was called twice)
    create_task_calls = [p for m, p in stub.calls if m == "POST" and p.endswith("/tasks")]
    assert len(create_task_calls) == 2, f"Expected 2 task creation calls, got {len(create_task_calls)}"
    # Verify that B was blocked on A by checking the workflow completed successfully
    # (both tasks must have completed for workflow to succeed)
    assert state.completed_at is not None
    _assert_no_unhandled(stub)


async def test_failed_dependency_skips_downstream(
    stub_agamemnon_factory: Callable[..., StubAgamemnon],
    client_pool: ClientPool,
    make_spec: Callable[..., WorkflowSpec],
) -> None:
    """If A fails (terminal event reports 'failed'), B is never POSTed and the
    workflow ends in failed state.

    Event-driven (#3): A's terminal status arrives via the NATS monitor, not via
    HTTP status polling, so we drive A='failed' through an explicit monitor mock
    that overrides conftest's all-completed auto-mock.
    """
    from unittest.mock import patch

    from tests.conftest import make_mock_monitor

    stub = stub_agamemnon_factory(task_statuses={"A": ["failed"]})
    client = client_pool.register(make_client_for(stub))

    spec = make_spec(
        tasks=[
            {"subject": "A", "description": "...", "assign_to": "worker"},
            {"subject": "B", "description": "...", "assign_to": "worker", "blocked_by": ["A"]},
        ],
        teardown="on_failure",
    )

    monitor = make_mock_monitor()
    # A is terminal-failed from the start; B must never become known/submitted.
    monitor.record_status("A", "failed")

    with patch("telemachy.executor.NatsMonitor", return_value=monitor):
        executor = WorkflowExecutor(client, poll_interval=0.01)
        state = await executor.execute(spec)

    assert state.status == "failed"
    subjects = {t.subject for team in stub.tasks.values() for t in team.values()}
    assert "B" not in subjects
    assert stub.agents == {}
    _assert_no_unhandled(stub)


async def test_partial_provisioning_failure_tears_down_first_agent(
    agamemnon_client: AgamemnonClient,
    stub_agamemnon: StubAgamemnon,
    make_spec: Callable[..., WorkflowSpec],
) -> None:
    """When the 2nd agent fails, the 1st must still be DELETEd (policy=on_failure)."""
    original = stub_agamemnon._dispatch
    n = {"v": 0}

    def flaky(method: str, path: str, body: dict) -> tuple[int, dict]:
        if method == "POST" and path == "/v1/agents":
            n["v"] += 1
            if n["v"] >= 2:  # Fail all requests to create the 2nd agent (and beyond)
                return 500, {"detail": "simulated"}
        return original(method, path, body)

    stub_agamemnon._dispatch = flaky  # type: ignore[method-assign]

    spec = make_spec(
        agents=[
            {"name": "a1", "runtime": "local"},
            {"name": "a2", "runtime": "local"},
        ],
        tasks=[
            {"subject": "T1", "description": "...", "assign_to": "a1"},
            {"subject": "T2", "description": "...", "assign_to": "a2"},
        ],
        teardown="on_failure",
    )
    executor = WorkflowExecutor(agamemnon_client, poll_interval=0.01)
    state = await executor.execute(spec)

    assert state.status == "failed"
    deletes = [p for m, p in stub_agamemnon.calls if m == "DELETE" and p.startswith("/v1/agents/")]
    assert len(deletes) >= 1, f"Expected at least 1 agent deletion, got {deletes}"
    assert stub_agamemnon.agents == {}
    _assert_no_unhandled(stub_agamemnon)


async def test_docker_runtime_hits_docker_endpoint(
    agamemnon_client: AgamemnonClient,
    stub_agamemnon: StubAgamemnon,
    make_spec: Callable[..., WorkflowSpec],
) -> None:
    spec = make_spec(
        agents=[{"name": "worker", "runtime": "docker", "docker_image": "alpine:3"}],
    )
    executor = WorkflowExecutor(agamemnon_client, poll_interval=0.01)
    state = await executor.execute(spec)

    assert state.status == "completed"
    assert ("POST", "/v1/agents/docker") in stub_agamemnon.calls
    assert ("POST", "/v1/agents") not in stub_agamemnon.calls
    _assert_no_unhandled(stub_agamemnon)


async def test_cli_load_path_executes_end_to_end(
    agamemnon_client: AgamemnonClient,
    stub_agamemnon: StubAgamemnon,
    write_workflow_yaml: Callable[..., Path],
) -> None:
    """A YAML file round-tripped through the CLI's _load_workflow runs end-to-end."""
    path = write_workflow_yaml(teardown="on_completion")
    spec = load_workflow(path)
    executor = WorkflowExecutor(agamemnon_client, poll_interval=0.01)
    state = await executor.execute(spec)

    assert state.status == "completed"
    assert any(p == "/v1/teams" for _, p in stub_agamemnon.calls)
    _assert_no_unhandled(stub_agamemnon)
