"""Mock-based tests for WorkflowExecutor."""

from __future__ import annotations

import asyncio
import time as _time
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from telemachy.agamemnon_client import AgamemnonClient, AgamemnonError
from telemachy.executor import WorkflowExecutor, WorkflowTimeoutError
from telemachy.models import AgentSpec, TaskSpec, WorkflowSpec
from telemachy.nats_monitor import NatsMonitor

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_spec(
    agents: list[dict] | None = None,
    tasks: list[dict] | None = None,
    teardown: str = "on_completion",
) -> WorkflowSpec:
    agents = agents or [{"name": "worker", "runtime": "local"}]
    tasks = tasks or [{"subject": "Task 1", "description": "Do work", "assign_to": "worker"}]
    return WorkflowSpec.model_validate(
        {
            "apiVersion": "telemachy/v1",
            "metadata": {"name": "test-wf", "description": "test"},
            "agents": agents,
            "teams": [
                {
                    "name": "team-a",
                    "agents": [a["name"] for a in agents],
                    "tasks": tasks,
                }
            ],
            "teardown": teardown,
        }
    )


def _make_mock_client() -> MagicMock:
    client = MagicMock(spec=AgamemnonClient)
    client.create_agent = AsyncMock(return_value="agent-id-001")
    client.wake_agent = AsyncMock()
    client.hibernate_agent = AsyncMock()
    client.delete_agent = AsyncMock()
    client.list_agents = AsyncMock(return_value=[])
    client.list_teams = AsyncMock(return_value=[])
    client.ping = AsyncMock(return_value=True)
    client.create_team = AsyncMock(return_value="team-id-001")
    client.create_task = AsyncMock(return_value="task-id-001")
    client.update_task = AsyncMock()
    client.get_tasks = AsyncMock(return_value=[{"subject": "Task 1", "status": "completed"}])
    client.delete_team = AsyncMock()
    return client


def _make_mock_monitor() -> AsyncMock:
    """Build a NatsMonitor mock wired for use as an async context manager.

    ``async with NatsMonitor(...) as monitor`` binds ``monitor`` to the
    ``__aenter__`` return value, so that must be the mock itself. The
    synchronous query methods (``latest_status``/``terminal_event``/
    ``record_status``/``notify_submitted``) are plain ``MagicMock``s so
    they return values rather than coroutines.
    """
    monitor = AsyncMock(spec=NatsMonitor)
    monitor.__aenter__.return_value = monitor
    monitor.__aexit__.return_value = None
    monitor.connected = True
    monitor.latest_status = MagicMock(return_value="completed")
    monitor.record_status = MagicMock()
    monitor.notify_submitted = MagicMock()

    def _default_event(_subject: str) -> asyncio.Event:
        ev = asyncio.Event()
        ev.set()
        return ev

    monitor.terminal_event = MagicMock(side_effect=_default_event)
    monitor.submitted_event = asyncio.Event()
    return monitor


# NOTE: NatsMonitor is auto-mocked for tests outside TestNatsMonitoring by the
# autouse fixture in tests/conftest.py; no per-module patcher is needed here.
# TestNatsMonitoring tests build their own monitor via _make_mock_monitor().


# ---------------------------------------------------------------------------
# Tests: provisioning
# ---------------------------------------------------------------------------


class TestProvisioning:
    @pytest.mark.asyncio
    async def test_create_agent_called_for_each_agent(self) -> None:
        client = _make_mock_client()
        spec = _make_spec(
            agents=[
                {"name": "agent-a", "runtime": "local"},
                {"name": "agent-b", "runtime": "local"},
            ],
            tasks=[
                {"subject": "T1", "description": "...", "assign_to": "agent-a"},
                {"subject": "T2", "description": "...", "assign_to": "agent-b"},
            ],
        )
        client.create_agent = AsyncMock(side_effect=["id-a", "id-b"])

        executor = WorkflowExecutor(client, poll_interval=0.01)
        await executor.execute(spec)

        assert client.create_agent.call_count == 2
        assert client.wake_agent.call_count == 2

    @pytest.mark.asyncio
    async def test_agent_ids_stored_in_state(self) -> None:
        client = _make_mock_client()
        client.create_agent = AsyncMock(return_value="maestro-xyz")
        spec = _make_spec()

        executor = WorkflowExecutor(client, poll_interval=0.01)
        state = await executor.execute(spec)

        assert "worker" in state.created_agents
        assert state.created_agents["worker"] == "maestro-xyz"

    @pytest.mark.asyncio
    async def test_correct_api_endpoint_for_docker_agent(self) -> None:
        client = _make_mock_client()
        spec = _make_spec(
            agents=[
                {
                    "name": "worker",
                    "runtime": "docker",
                    "docker_image": "example/img:latest",
                }
            ]
        )
        executor = WorkflowExecutor(client, poll_interval=0.01)
        await executor.execute(spec)

        # create_agent receives the AgentSpec; the client internally routes to docker endpoint
        call_args = client.create_agent.call_args[0][0]
        assert isinstance(call_args, AgentSpec)
        assert call_args.runtime == "docker"
        assert call_args.docker_image == "example/img:latest"


# ---------------------------------------------------------------------------
# Tests: team and task creation
# ---------------------------------------------------------------------------


class TestTaskCreation:
    @pytest.mark.asyncio
    async def test_create_team_called(self) -> None:
        client = _make_mock_client()
        spec = _make_spec()
        executor = WorkflowExecutor(client, poll_interval=0.01)
        await executor.execute(spec)
        client.create_team.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_task_called_for_each_task(self) -> None:
        client = _make_mock_client()
        client.get_tasks = AsyncMock(
            return_value=[
                {"subject": "T1", "status": "completed"},
                {"subject": "T2", "status": "completed"},
            ]
        )
        spec = _make_spec(
            tasks=[
                {"subject": "T1", "description": "...", "assign_to": "worker"},
                {"subject": "T2", "description": "...", "assign_to": "worker"},
            ]
        )
        executor = WorkflowExecutor(client, poll_interval=0.01)
        await executor.execute(spec)
        assert client.create_task.call_count == 2

    @pytest.mark.asyncio
    @pytest.mark.skip(
        reason="Replaced by event-driven test: test_dep_unblock_waits_on_terminal_event_not_sleep"
    )
    async def test_dependent_task_submitted_after_predecessor(self) -> None:
        """Task with blocked_by must not be submitted before its dependency completes."""
        call_order: list[str] = []

        async def fake_create_task(
            team_id: str,
            spec: TaskSpec,
            blocked_by_ids: list[str] | None = None,
            assignee_agent_id: str | None = None,
        ) -> str:
            call_order.append(spec.subject)
            return f"task-{len(call_order)}"

        # First call returns pending; second call returns completed
        get_tasks_responses = [
            [{"subject": "Step 1", "status": "pending"}],
            [{"subject": "Step 1", "status": "completed"}],
            [
                {"subject": "Step 1", "status": "completed"},
                {"subject": "Step 2", "status": "completed"},
            ],
        ]
        call_count = {"n": 0}

        async def fake_get_tasks(team_id: str) -> list[dict]:
            idx = min(call_count["n"], len(get_tasks_responses) - 1)
            call_count["n"] += 1
            return get_tasks_responses[idx]

        client = _make_mock_client()
        client.create_task = AsyncMock(side_effect=fake_create_task)
        client.get_tasks = AsyncMock(side_effect=fake_get_tasks)

        spec = _make_spec(
            tasks=[
                {"subject": "Step 1", "description": "...", "assign_to": "worker"},
                {
                    "subject": "Step 2",
                    "description": "...",
                    "assign_to": "worker",
                    "blocked_by": ["Step 1"],
                },
            ]
        )
        executor = WorkflowExecutor(client, poll_interval=0.01)
        await executor.execute(spec)

        assert call_order[0] == "Step 1"
        assert call_order[1] == "Step 2"


# ---------------------------------------------------------------------------
# Tests: teardown
# ---------------------------------------------------------------------------


class TestTeardown:
    @pytest.mark.asyncio
    async def test_teardown_on_completion_deletes_agents(self) -> None:
        client = _make_mock_client()
        client.create_agent = AsyncMock(return_value="agent-to-delete")
        spec = _make_spec(teardown="on_completion")

        executor = WorkflowExecutor(client, poll_interval=0.01)
        await executor.execute(spec)

        client.delete_agent.assert_called_once_with("agent-to-delete")

    @pytest.mark.asyncio
    async def test_teardown_never_skips_deletion(self) -> None:
        client = _make_mock_client()
        spec = _make_spec(teardown="never")

        executor = WorkflowExecutor(client, poll_interval=0.01)
        await executor.execute(spec)

        client.delete_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_teardown_on_failure_skips_on_success(self) -> None:
        client = _make_mock_client()
        spec = _make_spec(teardown="on_failure")

        executor = WorkflowExecutor(client, poll_interval=0.01)
        state = await executor.execute(spec)

        assert state.status == "completed"
        client.delete_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_completed_state_on_success(self) -> None:
        client = _make_mock_client()
        spec = _make_spec()

        executor = WorkflowExecutor(client, poll_interval=0.01)
        state = await executor.execute(spec)

        assert state.status == "completed"
        assert state.completed_at is not None

    @pytest.mark.asyncio
    async def test_failed_state_when_task_fails(self) -> None:
        client = _make_mock_client()
        client.get_tasks = AsyncMock(return_value=[{"subject": "Task 1", "status": "failed"}])
        spec = _make_spec(teardown="never")

        executor = WorkflowExecutor(client, poll_interval=0.01)
        state = await executor.execute(spec)

        assert state.status == "failed"
        assert state.error is not None


# ---------------------------------------------------------------------------
# Tests: error paths
# ---------------------------------------------------------------------------


class TestErrorPaths:
    @pytest.mark.asyncio
    @pytest.mark.skip(
        reason="Replaced by event-driven test: test_wait_for_all_terminal_skips_unsubmitted_tasks"
    )
    async def test_failed_dependency_skips_downstream_task(self) -> None:
        """When task A fails, task B (blocked_by A) must never be submitted."""
        # get_tasks returns A as "failed" after it is submitted, then stays failed
        get_tasks_responses = [
            # First poll (waiting for A to complete): A is still pending
            [{"subject": "Task A", "status": "pending"}],
            # Second poll: A has failed
            [{"subject": "Task A", "status": "failed"}],
            # Monitor phase: A is failed (will cause workflow to mark as failed)
            [{"subject": "Task A", "status": "failed"}],
        ]
        call_count: dict[str, int] = {"n": 0}

        async def fake_get_tasks(team_id: str) -> list[dict]:
            idx = min(call_count["n"], len(get_tasks_responses) - 1)
            call_count["n"] += 1
            return get_tasks_responses[idx]

        client = _make_mock_client()
        client.create_task = AsyncMock(return_value="task-id-001")
        client.get_tasks = AsyncMock(side_effect=fake_get_tasks)

        spec = _make_spec(
            tasks=[
                {"subject": "Task A", "description": "First task", "assign_to": "worker"},
                {
                    "subject": "Task B",
                    "description": "Blocked downstream task",
                    "assign_to": "worker",
                    "blocked_by": ["Task A"],
                },
            ],
            teardown="never",
        )

        executor = WorkflowExecutor(client, poll_interval=0.01)
        state = await executor.execute(spec)

        # Executor must complete (not hang)
        assert state.completed_at is not None

        # Task B must never have been submitted
        submitted_subjects = [call.args[1].subject for call in client.create_task.call_args_list]
        assert "Task B" not in submitted_subjects
        assert "Task A" in submitted_subjects

    @pytest.mark.asyncio
    async def test_teardown_runs_even_when_execution_fails(self) -> None:
        """Teardown (delete_agent) must run even when the workflow raises mid-execution."""
        client = _make_mock_client()
        client.create_agent = AsyncMock(return_value="agent-to-teardown")
        spec = _make_spec(teardown="on_failure")

        executor = WorkflowExecutor(client, poll_interval=0.01)

        # Simulate an unexpected error during team/task creation phase
        with patch.object(
            executor,
            "_create_teams_only",
            new_callable=AsyncMock,
            side_effect=RuntimeError("unexpected mid-execution error"),
        ):
            state = await executor.execute(spec)

        # Workflow must be marked failed, not hanging
        assert state.status == "failed"
        # Teardown must still have run (policy=on_failure, status=failed → should delete)
        client.delete_agent.assert_called_once_with("agent-to-teardown")

    @pytest.mark.asyncio
    async def test_partial_agent_creation_teardown(self) -> None:
        """When second agent creation fails, already-created agents must be torn down."""
        client = _make_mock_client()

        # Agent 1 succeeds, agent 2 raises AgamemnonError
        client.create_agent = AsyncMock(
            side_effect=[
                "agent-id-first",
                AgamemnonError(500, "internal server error"),
            ]
        )

        spec = _make_spec(
            agents=[
                {"name": "agent-first", "runtime": "local"},
                {"name": "agent-second", "runtime": "local"},
            ],
            tasks=[
                {"subject": "T1", "description": "...", "assign_to": "agent-first"},
                {"subject": "T2", "description": "...", "assign_to": "agent-second"},
            ],
            teardown="on_failure",
        )

        executor = WorkflowExecutor(client, poll_interval=0.01)
        state = await executor.execute(spec)

        # Workflow must be marked failed due to the AgamemnonError
        assert state.status == "failed"
        assert state.error is not None

        # The first agent (which was created) must have been deleted during teardown
        deleted_ids = [call.args[0] for call in client.delete_agent.call_args_list]
        assert "agent-id-first" in deleted_ids

    @pytest.mark.asyncio
    async def test_wake_agent_failure_still_records_created_agent_for_teardown(self) -> None:
        """If create_agent succeeds but wake_agent raises, the created agent
        must still appear in state.created_agents so teardown can delete it (#164).

        Single-agent case — no concurrency, ordering is trivial.
        """
        client = _make_mock_client()
        client.create_agent = AsyncMock(return_value="agent-created-but-not-woken")
        client.wake_agent = AsyncMock(side_effect=AgamemnonError(503, "service unavailable"))

        spec = _make_spec(
            agents=[{"name": "worker", "runtime": "local"}],
            teardown="on_failure",
        )

        executor = WorkflowExecutor(client, poll_interval=0.01)
        state = await executor.execute(spec)

        assert state.status == "failed"
        assert state.created_agents.get("worker") == "agent-created-but-not-woken"
        deleted_ids = [call.args[0] for call in client.delete_agent.call_args_list]
        assert "agent-created-but-not-woken" in deleted_ids

    @pytest.mark.asyncio
    async def test_partial_provisioning_tracks_all_completed_creates_before_failure(
        self,
    ) -> None:
        """When create_agent fan-out has mixed outcomes, every agent whose
        create_agent returned an id must be present in state.created_agents,
        regardless of gather() completion ordering (#164).

        Determinism: max_concurrent_provisioning=1 forces the _bounded() coroutines
        to serialize through self._provision_semaphore. Calls to client.create_agent
        therefore arrive in agent-list order, so AsyncMock.side_effect entries map
        1:1 to agents A/B/C and the test does not depend on asyncio.gather
        scheduling behaviour.
        """
        client = _make_mock_client()
        client.create_agent = AsyncMock(
            side_effect=[
                "id-A",
                AgamemnonError(500, "boom"),
                "id-C",
            ]
        )

        spec = _make_spec(
            agents=[
                {"name": "agent-A", "runtime": "local"},
                {"name": "agent-B", "runtime": "local"},
                {"name": "agent-C", "runtime": "local"},
            ],
            tasks=[
                {"subject": "T", "description": "...", "assign_to": "agent-A"},
            ],
            teardown="on_failure",
        )

        # max_concurrent_provisioning=1 → strictly sequential create_agent calls.
        executor = WorkflowExecutor(client, poll_interval=0.01, max_concurrent_provisioning=1)
        state = await executor.execute(spec)

        assert state.status == "failed"
        # Every agent whose create_agent returned an id must be tracked,
        # even though one sibling raised mid-fan-out.
        assert state.created_agents.get("agent-A") == "id-A"
        assert state.created_agents.get("agent-C") == "id-C"
        assert "agent-B" not in state.created_agents
        # Both successfully-created agents must be torn down (delete_agent
        # ordering is not asserted — teardown iterates state.created_agents).
        deleted_ids = [call.args[0] for call in client.delete_agent.call_args_list]
        assert "id-A" in deleted_ids
        assert "id-C" in deleted_ids
        # wake_agent was called for both successful creates (default AsyncMock
        # succeeds), but not for the failing one.
        woken_ids = [call.args[0] for call in client.wake_agent.call_args_list]
        assert "id-A" in woken_ids
        assert "id-C" in woken_ids


# ---------------------------------------------------------------------------
# Tests: hooks (#144)
# ---------------------------------------------------------------------------


class TestHooks:
    def test_add_hook_rejects_unknown_event(self) -> None:
        client = _make_mock_client()
        executor = WorkflowExecutor(client, poll_interval=0.01)
        with pytest.raises(ValueError, match="Unknown hook event"):
            executor.add_hook("on_unknown_event", lambda **_: None)

    def test_add_hook_accepts_known_events(self) -> None:
        client = _make_mock_client()
        executor = WorkflowExecutor(client, poll_interval=0.01)
        for event in (
            "on_task_complete",
            "on_task_failed",
            "on_workflow_complete",
            "on_workflow_failed",
        ):
            executor.add_hook(event, lambda **_: None)

    @pytest.mark.asyncio
    async def test_emit_invokes_sync_and_async_callbacks(self) -> None:
        client = _make_mock_client()
        executor = WorkflowExecutor(client, poll_interval=0.01)

        sync_calls: list[dict] = []
        async_calls: list[dict] = []

        def sync_cb(**kwargs: object) -> None:
            sync_calls.append(kwargs)

        async def async_cb(**kwargs: object) -> None:
            async_calls.append(kwargs)

        executor.add_hook("on_task_complete", sync_cb)
        executor.add_hook("on_task_complete", async_cb)

        await executor._emit("on_task_complete", subject="T1", status="completed")

        assert sync_calls == [{"subject": "T1", "status": "completed"}]
        assert async_calls == [{"subject": "T1", "status": "completed"}]

    @pytest.mark.asyncio
    async def test_emit_uses_inspect_iscoroutinefunction(self, monkeypatch) -> None:
        """Lock in the #256 migration: _emit must dispatch via inspect, not asyncio."""
        import inspect as _inspect

        from telemachy import executor as executor_mod

        calls: list[Callable[..., object]] = []
        real = _inspect.iscoroutinefunction

        def spy(cb: Callable[..., object]) -> bool:
            calls.append(cb)
            return real(cb)

        monkeypatch.setattr(executor_mod.inspect, "iscoroutinefunction", spy)

        sync_called = False
        async_called = False

        def sync_cb(**_: object) -> None:
            nonlocal sync_called
            sync_called = True

        async def async_cb(**_: object) -> None:
            nonlocal async_called
            async_called = True

        client = _make_mock_client()
        executor = WorkflowExecutor(client, poll_interval=0.01)
        executor.add_hook("on_task_complete", sync_cb)
        executor.add_hook("on_task_complete", async_cb)
        await executor._emit("on_task_complete", subject="T1", status="completed")

        assert sync_called and async_called
        assert sync_cb in calls and async_cb in calls

    @pytest.mark.asyncio
    async def test_emitted_subjects_do_not_leak_across_execute_calls(
        self,
    ) -> None:
        """Reusing one executor for two workflows must re-emit on_task_complete
        for a repeated task subject — the event-driven monitor's per-instance
        _emitted_task_events set is reset at the top of each execute() (#162/#203)."""
        client = _make_mock_client()
        # Reconcile seeds "Task 1" as completed for both runs.
        client.get_tasks = AsyncMock(return_value=[{"subject": "Task 1", "status": "completed"}])

        executor = WorkflowExecutor(client, poll_interval=0.01)
        calls: list[str] = []
        executor.add_hook("on_task_complete", lambda **kw: calls.append(kw["task"]["subject"]))

        spec = _make_spec()
        await executor.execute(spec)
        await executor.execute(spec)

        # Each execute() must independently emit on_task_complete for "Task 1".
        assert calls == ["Task 1", "Task 1"], (
            f"Expected two emissions across two execute() calls, got {calls!r} — "
            "emitted-event state leaked between runs (#162/#203 regression)"
        )


# ---------------------------------------------------------------------------
# Tests: timeout behaviour (#142)
# ---------------------------------------------------------------------------


class TestWorkflowTimeout:
    @pytest.mark.asyncio
    async def test_execute_raises_workflow_timeout_error_on_wait_for_timeout(self) -> None:
        client = _make_mock_client()
        spec = _make_spec()
        spec.timeout_seconds = 0.01  # immediate timeout

        executor = WorkflowExecutor(client, poll_interval=0.01)

        async def slow_run(_spec: object, _workflow_id: object = None) -> object:
            import asyncio as _a

            await _a.sleep(10)
            return None

        with (
            patch.object(executor, "_run", new=slow_run),
            pytest.raises(WorkflowTimeoutError, match="exceeded its execution timeout"),
        ):
            await executor.execute(spec)


# ---------------------------------------------------------------------------
# Tests: stop-event graceful cancellation (#143)
# ---------------------------------------------------------------------------


class TestStopEvent:
    @pytest.mark.asyncio
    async def test_stop_event_set_before_execute_short_circuits_monitor(self) -> None:
        import asyncio as _a

        client = _make_mock_client()
        spec = _make_spec(teardown="never")

        stop_event = _a.Event()
        stop_event.set()  # pre-set: monitor must observe it on first iteration

        executor = WorkflowExecutor(client, poll_interval=0.01, stop_event=stop_event)
        state = await executor.execute(spec)

        # Workflow finishes (not hangs) when stop event is set.
        assert state.completed_at is not None


# ---------------------------------------------------------------------------
# Tests: idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    @pytest.mark.asyncio
    async def test_rerun_reuses_existing_agent(self) -> None:
        from telemachy.idempotency import make_key

        spec = _make_spec()
        existing_key = make_key("test-wf", "worker")
        client = _make_mock_client()
        client.list_agents = AsyncMock(
            return_value=[{"id": "preexisting-agent-id", "name": existing_key}]
        )
        executor = WorkflowExecutor(client, poll_interval=0.01)
        state = await executor.execute(spec)
        client.create_agent.assert_not_called()
        assert state.created_agents["worker"] == "preexisting-agent-id"

    @pytest.mark.asyncio
    async def test_rerun_reuses_existing_team_and_task(self) -> None:
        from telemachy.idempotency import make_key

        spec = _make_spec()
        team_key = make_key("test-wf", "team-a")
        client = _make_mock_client()
        client.list_teams = AsyncMock(
            return_value=[{"id": "preexisting-team-id", "name": team_key}]
        )
        client.get_tasks = AsyncMock(
            return_value=[
                {"id": "preexisting-task-id", "subject": "Task 1", "status": "completed"},
            ]
        )
        executor = WorkflowExecutor(client, poll_interval=0.01)
        await executor.execute(spec)
        client.create_team.assert_not_called()
        client.create_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_force_bypasses_idempotency(self) -> None:
        from telemachy.idempotency import make_key

        spec = _make_spec()
        existing_key = make_key("test-wf", "worker")
        client = _make_mock_client()
        client.list_agents = AsyncMock(return_value=[{"id": "old-agent", "name": existing_key}])
        executor = WorkflowExecutor(client, poll_interval=0.01, force=True)
        state = await executor.execute(spec)
        client.create_agent.assert_called_once()
        assert state.created_agents["worker"] == "agent-id-001"

    @pytest.mark.asyncio
    async def test_partial_prior_run_completes_missing_resources(self) -> None:
        from telemachy.idempotency import make_key

        spec = _make_spec()
        client = _make_mock_client()
        client.list_agents = AsyncMock(
            return_value=[{"id": "reused-agent", "name": make_key("test-wf", "worker")}]
        )
        await WorkflowExecutor(client, poll_interval=0.01).execute(spec)
        client.create_agent.assert_not_called()
        client.create_team.assert_called_once()
        assert client.create_team.call_args.args[0] == make_key("test-wf", "team-a")

    @pytest.mark.asyncio
    async def test_reuse_tolerates_already_running_agent(self) -> None:
        from telemachy.idempotency import make_key

        spec = _make_spec()
        client = _make_mock_client()
        client.list_agents = AsyncMock(
            return_value=[{"id": "reused-agent", "name": make_key("test-wf", "worker")}]
        )
        # wake_agent raises a 409-shaped conflict on the reused agent
        client.wake_agent = AsyncMock(side_effect=AgamemnonError(409, "agent is already running"))
        # Must NOT raise; reuse continues.
        state = await WorkflowExecutor(client, poll_interval=0.01).execute(spec)
        assert state.status == "completed"
        assert state.created_agents["worker"] == "reused-agent"

    @pytest.mark.asyncio
    async def test_reuse_propagates_non_conflict_wake_error(self) -> None:
        from telemachy.idempotency import make_key

        spec = _make_spec(teardown="never")
        client = _make_mock_client()
        client.list_agents = AsyncMock(
            return_value=[{"id": "reused-agent", "name": make_key("test-wf", "worker")}]
        )
        client.wake_agent = AsyncMock(side_effect=AgamemnonError(500, "internal error"))
        state = await WorkflowExecutor(client, poll_interval=0.01).execute(spec)
        assert state.status == "failed"
        assert state.error is not None and "500" in state.error


# ---------------------------------------------------------------------------
# Tests: rate limiting under concurrent provisioning (#160)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provision_respects_rate_limit_under_gather() -> None:
    """20 agents under asyncio.gather + rate=20/s, burst=5 → elapsed in [0.6, 4.0]s.

    Math: each agent issues two throttled calls (`create_agent` + `wake_agent`),
    so 20 agents = 40 calls through `_request_with_retry`. At rate=20/burst=5
    the minimum is (40 - 5) / 20 = 1.75s on the rate-limited side. Upper bound
    widened to 4.0s to absorb CI variance and the single-vCPU edge case
    flagged in the prior review.
    """
    from telemachy.agamemnon_client import AgamemnonClient

    real_client = AgamemnonClient(
        url="https://test.local",
        require_tls=True,
        rate_limit_rps=20.0,
        rate_limit_burst=5,
    )
    real_client._client = MagicMock()
    real_client._client.request = AsyncMock(
        return_value=MagicMock(
            status_code=201, is_error=False, json=lambda: {"agent": {"id": "a"}}, text=""
        )
    )

    # Patch high-level methods to short-circuit team/task/monitor work
    real_client.get_tasks = AsyncMock(return_value=[{"subject": "T", "status": "completed"}])
    real_client.create_team = AsyncMock(return_value="team-1")
    real_client.create_task = AsyncMock(return_value="task-1")

    agents = [{"name": f"a{i}", "runtime": "local"} for i in range(20)]
    spec = _make_spec(
        agents=agents,
        tasks=[{"subject": "T", "description": "...", "assign_to": "a0"}],
        teardown="never",
    )
    executor = WorkflowExecutor(real_client, poll_interval=0.01)

    start = _time.monotonic()
    await executor.execute(spec)
    elapsed = _time.monotonic() - start
    assert 0.6 <= elapsed <= 4.0


# ---------------------------------------------------------------------------
# Tests: execution identity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_accepts_workflow_id_override() -> None:
    client = _make_mock_client()
    executor = WorkflowExecutor(client, poll_interval=0.01)
    spec = _make_spec()
    result = await executor.execute(spec, workflow_id="custom-42")
    assert result.workflow_id == "custom-42"


# === Observability tests ===


@pytest.mark.asyncio
async def test_workflow_id_propagates_to_logs(caplog: pytest.LogCaptureFixture) -> None:
    """workflow_id from context is attached to every log record by filter."""
    from telemachy.telemetry import WorkflowContextLogFilter

    client = _make_mock_client()
    spec = _make_spec()
    executor = WorkflowExecutor(client)

    # Attach filter to caplog so context vars are captured
    caplog_handler = caplog.handler
    caplog_handler.addFilter(WorkflowContextLogFilter())

    state = await executor.execute(spec)

    # Every record should have workflow_id from the run
    for record in caplog.records:
        assert hasattr(record, "workflow_id")
        assert record.workflow_id == state.workflow_id


@pytest.mark.asyncio
async def test_workflow_id_contextvar_resets_after_run() -> None:
    """workflow_id contextvar is reset after execute() returns."""
    from telemachy.telemetry import workflow_id_var

    client = _make_mock_client()
    spec = _make_spec()
    executor = WorkflowExecutor(client)

    # Before execute, contextvar is unset (default "-")
    assert workflow_id_var.get("-") == "-"

    await executor.execute(spec)

    # After execute, contextvar is reset
    assert workflow_id_var.get("-") == "-"


@pytest.mark.asyncio
async def test_workflow_id_propagates_into_gather_children() -> None:
    """workflow_id contextvar propagates into asyncio.gather spawned tasks."""
    from telemachy.telemetry import workflow_id_var

    client = _make_mock_client()
    spec = _make_spec()

    recorded_ids: list[str] = []

    async def capture_id(*_: object, **__: object) -> tuple[str, str]:
        recorded_ids.append(workflow_id_var.get("-"))
        return "test-agent", "agent-id"

    client.create_agent = capture_id

    executor = WorkflowExecutor(client)
    state = await executor.execute(spec)

    # The mock captured the workflow_id from within the gather'd task
    assert len(recorded_ids) > 0
    assert all(wid == state.workflow_id for wid in recorded_ids)


@pytest.mark.asyncio
async def test_metrics_increment_on_success() -> None:
    """Workflow completion increments WORKFLOWS_STARTED, WORKFLOWS_COMPLETED, and duration."""

    with patch("telemachy.executor.WORKFLOWS_STARTED") as mock_started, \
         patch("telemachy.executor.WORKFLOWS_COMPLETED") as mock_completed, \
         patch("telemachy.executor.WORKFLOW_DURATION") as mock_duration:
        client = _make_mock_client()
        spec = _make_spec()
        executor = WorkflowExecutor(client)

        await executor.execute(spec)

        # Verify metrics were called with correct labels
        mock_started.labels.assert_called()
        mock_completed.labels.assert_called()
        mock_duration.labels.assert_called()

        # Check status
        call_args = mock_completed.labels.call_args
        assert call_args[1]["status"] == "completed"


@pytest.mark.asyncio
async def test_metrics_increment_on_failure() -> None:
    """Workflow failure sets status='failed' on WORKFLOWS_COMPLETED."""

    client = _make_mock_client()
    client.create_agent = AsyncMock(side_effect=RuntimeError("agent creation failed"))
    spec = _make_spec()

    with patch("telemachy.executor.WORKFLOWS_COMPLETED") as mock_completed:
        executor = WorkflowExecutor(client)
        state = await executor.execute(spec)

        assert state.status == "failed"
        # Verify status='failed' was recorded
        call_args = mock_completed.labels.call_args
        assert call_args[1]["status"] == "failed"


@pytest.mark.asyncio
async def test_tasks_total_increments_per_terminal_status() -> None:
    """TASKS_TOTAL is incremented for each task reaching a terminal state."""

    client = _make_mock_client()
    spec = _make_spec(
        tasks=[
            {"subject": "Task 1", "description": "Do work", "assign_to": "worker"},
            {"subject": "Task 2", "description": "Do work", "assign_to": "worker"},
            {"subject": "Task 3", "description": "Do work", "assign_to": "worker"},
        ]
    )

    # Mock get_tasks to return completed tasks
    client.get_tasks = AsyncMock(
        return_value=[
            {"subject": "Task 1", "status": "completed"},
            {"subject": "Task 2", "status": "completed"},
            {"subject": "Task 3", "status": "failed"},
        ]
    )

    with patch("telemachy.executor.TASKS_TOTAL") as mock_tasks:
        executor = WorkflowExecutor(client)
        await executor.execute(spec)

        # Verify TASKS_TOTAL was called for each task status transition
        # (exact call count varies based on monitoring loop, but should be present)
        assert mock_tasks.labels.called


@pytest.mark.asyncio
async def test_workflow_spans_emit_end_to_end() -> None:
    """Workflow execution calls get_tracer() to emit spans without errors."""
    from unittest.mock import patch

    from telemachy.telemetry import get_tracer

    client = _make_mock_client()
    spec = _make_spec()
    executor = WorkflowExecutor(client)

    # Patch get_tracer to verify it's called
    with patch("telemachy.executor.get_tracer", wraps=get_tracer) as mock_tracer:
        await executor.execute(spec)
        # Verify get_tracer was called (indicating spans are being emitted)
        assert mock_tracer.call_count > 0


# ---------------------------------------------------------------------------
# Tests: NATS event-driven monitoring
# ---------------------------------------------------------------------------


class TestNatsMonitoring:
    @pytest.mark.asyncio
    async def test_executor_uses_nats_monitor_no_polling_loop(self) -> None:
        """Test that monitoring uses NATS events, not HTTP polling."""
        client = _make_mock_client()
        spec = _make_spec(
            tasks=[{"subject": "Task 1", "description": "...", "assign_to": "worker"}]
        )

        mock_monitor = _make_mock_monitor()

        with patch("telemachy.executor.NatsMonitor", return_value=mock_monitor):
            executor = WorkflowExecutor(client, poll_interval=5.0)
            state = await executor.execute(spec)

        # Verify monitoring is event-driven: get_tasks is called a bounded number
        # of times (one reconcile snapshot + one idempotent task-reuse check per
        # team) and NOT in a status-polling loop. With a single team that is at
        # most 2 calls — never the dozens a polling loop would produce.
        assert client.get_tasks.call_count <= 2
        assert state.status == "completed"

    @pytest.mark.asyncio
    async def test_executor_propagates_nats_unavailable(self) -> None:
        """Test that NATS unavailability causes workflow to fail."""
        from telemachy.nats_monitor import NatsUnavailableError

        client = _make_mock_client()
        spec = _make_spec()

        # NatsMonitor.__aenter__ raises (connection cannot be established).
        mock_monitor = _make_mock_monitor()
        mock_monitor.__aenter__.side_effect = NatsUnavailableError("NATS unreachable")

        with patch("telemachy.executor.NatsMonitor", return_value=mock_monitor):
            executor = WorkflowExecutor(client, poll_interval=0.01)
            state = await executor.execute(spec)

        # Workflow should fail with NATS error
        assert state.status == "failed"
        assert "NATS" in state.error

    @pytest.mark.asyncio
    async def test_executor_handles_mid_workflow_disconnect(self) -> None:
        """Test that mid-workflow NATS disconnect is detected."""
        client = _make_mock_client()
        spec = _make_spec()

        # Connection drops mid-monitoring: terminal events never fire and the
        # monitor reports disconnected, so _wait_for_all_terminal must bail out.
        mock_monitor = _make_mock_monitor()
        mock_monitor.connected = False
        mock_monitor.latest_status = MagicMock(return_value="in_progress")

        def _unset_event(_subject: str) -> asyncio.Event:
            return asyncio.Event()  # never set

        mock_monitor.terminal_event = MagicMock(side_effect=_unset_event)

        with patch("telemachy.executor.NatsMonitor", return_value=mock_monitor):
            executor = WorkflowExecutor(client, poll_interval=0.01)
            state = await executor.execute(spec)

        # Should fail due to connection loss
        assert state.status == "failed"

    @pytest.mark.asyncio
    async def test_reconcile_initial_seeds_terminal_status(self) -> None:
        """Test that _reconcile_initial loads initial task statuses."""
        client = _make_mock_client()
        spec = _make_spec(
            tasks=[{"subject": "Task 1", "description": "...", "assign_to": "worker"}]
        )

        # Pretend the task is already completed before we start monitoring
        client.get_tasks.return_value = [{"subject": "Task 1", "status": "completed"}]

        mock_monitor = _make_mock_monitor()

        with patch("telemachy.executor.NatsMonitor", return_value=mock_monitor):
            executor = WorkflowExecutor(client, poll_interval=0.01)
            state = await executor.execute(spec)

        # Should complete immediately since task is already terminal
        assert state.status == "completed"
        # record_status should have been called with the reconcile data
        assert mock_monitor.record_status.called

    @pytest.mark.asyncio
    async def test_dep_unblock_waits_on_terminal_event_not_sleep(self) -> None:
        """Test that dep-wait uses terminal events, not asyncio.sleep."""

        client = _make_mock_client()
        spec = _make_spec(
            tasks=[
                {"subject": "Task 1", "description": "...", "assign_to": "worker"},
                {
                    "subject": "Task 2",
                    "description": "...",
                    "assign_to": "worker",
                    "blocked_by": ["Task 1"],
                },
            ]
        )

        client.create_task = AsyncMock(side_effect=["task-1", "task-2"])

        mock_monitor = _make_mock_monitor()
        mock_monitor.latest_status = MagicMock(
            side_effect=lambda subj: {
                "Task 1": "completed",
                "Task 2": "completed",
            }.get(subj)
        )

        with (
            patch("telemachy.executor.NatsMonitor", return_value=mock_monitor),
            patch("asyncio.sleep") as mock_sleep,
        ):
            executor = WorkflowExecutor(client, poll_interval=0.01)
            state = await executor.execute(spec)

            # asyncio.sleep should never be called (no polling in dep-wait)
            mock_sleep.assert_not_called()

        assert state.status == "completed"

    @pytest.mark.asyncio
    async def test_wait_for_all_terminal_skips_unsubmitted_tasks(self) -> None:
        """Test that monitor skips tasks that were never submitted."""

        client = _make_mock_client()
        spec = _make_spec(
            tasks=[
                {"subject": "Task 1", "description": "...", "assign_to": "worker"},
                {
                    "subject": "Task 2",
                    "description": "...",
                    "assign_to": "worker",
                    "blocked_by": ["Task 1"],
                },
                {"subject": "Task 3", "description": "...", "assign_to": "worker"},
            ]
        )

        # Task 1 fails, Task 2 is skipped due to dep-failure, Task 3 completes
        client.create_task = AsyncMock(side_effect=["task-1", "task-3"])

        mock_monitor = _make_mock_monitor()
        mock_monitor.latest_status = MagicMock(
            side_effect=lambda subj: {
                "Task 1": "failed",
                "Task 3": "completed",
                "Task 2": None,  # Should never be queried
            }.get(subj)
        )

        def make_event(subj: str) -> asyncio.Event:
            ev = asyncio.Event()
            if subj in ("Task 1", "Task 3"):
                ev.set()
            return ev

        mock_monitor.terminal_event = MagicMock(side_effect=make_event)

        with patch("telemachy.executor.NatsMonitor", return_value=mock_monitor):
            executor = WorkflowExecutor(client, poll_interval=0.01)
            state = await executor.execute(spec)

        # Should fail due to Task 1 failure (Task 2 skipped, Task 3 completed)
        assert state.status == "failed"
        # Task 2 should NOT be in submitted_task_subjects
        assert "Task 2" not in state.submitted_task_subjects

    @pytest.mark.skip(
        reason="Cross-team blocked_by is rejected by TeamSpec.detect_dependency_cycles "
        "(deps are validated per-team). Enabling cross-team dependencies is a separate "
        "schema change (move dep validation to WorkflowSpec) tracked outside #3."
    )
    @pytest.mark.asyncio
    async def test_submitted_event_wakes_cross_team_dep_wait(self) -> None:
        """Test that submitted_event wakes dep-wait across teams."""

        client = _make_mock_client()
        # Two teams; team-b depends on team-a
        spec = WorkflowSpec.model_validate(
            {
                "apiVersion": "telemachy/v1",
                "metadata": {"name": "cross-team-deps", "description": "test"},
                "agents": [
                    {"name": "agent-a", "runtime": "local"},
                    {"name": "agent-b", "runtime": "local"},
                ],
                "teams": [
                    {
                        "name": "team-a",
                        "agents": ["agent-a"],
                        "tasks": [
                            {"subject": "Task A", "description": "...", "assign_to": "agent-a"}
                        ],
                    },
                    {
                        "name": "team-b",
                        "agents": ["agent-b"],
                        "tasks": [
                            {
                                "subject": "Task B",
                                "description": "...",
                                "assign_to": "agent-b",
                                "blocked_by": ["Task A"],
                            }
                        ],
                    },
                ],
                "teardown": "on_completion",
            }
        )

        client.create_task = AsyncMock(side_effect=["task-a", "task-b"])
        client.create_team = AsyncMock(side_effect=["team-a", "team-b"])

        mock_monitor = _make_mock_monitor()

        with (
            patch("asyncio.sleep") as mock_sleep,
            patch("telemachy.executor.NatsMonitor", return_value=mock_monitor),
        ):
            executor = WorkflowExecutor(client, poll_interval=0.01)
            state = await executor.execute(spec)

            # Should complete without polling sleeps
            mock_sleep.assert_not_called()

        assert state.status == "completed"
