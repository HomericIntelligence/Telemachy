"""Mock-based tests for WorkflowExecutor."""

from __future__ import annotations

import asyncio
import time as _time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from telemachy.agamemnon_client import AgamemnonError
from telemachy.executor import WorkflowExecutor, WorkflowTimeoutError
from telemachy.models import AgentSpec
from tests.conftest import make_executor_spec, make_mock_client, make_mock_monitor

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


# NOTE: NatsMonitor is auto-mocked for tests outside TestNatsMonitoring by the
# autouse fixture in tests/conftest.py; no per-module patcher is needed here.
# TestNatsMonitoring tests build their own monitor via make_mock_monitor().


# ---------------------------------------------------------------------------
# Tests: provisioning
# ---------------------------------------------------------------------------


class TestProvisioning:
    @pytest.mark.asyncio
    async def test_every_agent_created_woken_and_tracked_by_name(self) -> None:
        """Each agent is created, woken, and recorded under its workflow name."""
        client = make_mock_client()
        client.create_agent = AsyncMock(side_effect=["id-a", "id-b"])
        spec = make_executor_spec(
            agents=[
                {"name": "agent-a", "runtime": "local"},
                {"name": "agent-b", "runtime": "local"},
            ],
            tasks=[
                {"subject": "T1", "description": "...", "assign_to": "agent-a"},
                {"subject": "T2", "description": "...", "assign_to": "agent-b"},
            ],
        )

        state = await WorkflowExecutor(client, poll_interval=0.01).execute(spec)

        assert state.created_agents == {"agent-a": "id-a", "agent-b": "id-b"}
        assert [call.args[0] for call in client.wake_agent.call_args_list] == ["id-a", "id-b"]

    @pytest.mark.asyncio
    async def test_correct_api_endpoint_for_docker_agent(self) -> None:
        client = make_mock_client()
        spec = make_executor_spec(
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
    async def test_one_team_and_every_task_submitted_with_its_assignee(self) -> None:
        """Team provisioning happens once; each task is submitted under its subject."""
        client = make_mock_client()
        client.get_tasks = AsyncMock(
            return_value=[
                {"subject": "T1", "status": "completed"},
                {"subject": "T2", "status": "completed"},
            ]
        )
        spec = make_executor_spec(
            tasks=[
                {"subject": "T1", "description": "...", "assign_to": "worker"},
                {"subject": "T2", "description": "...", "assign_to": "worker"},
            ]
        )

        await WorkflowExecutor(client, poll_interval=0.01).execute(spec)

        client.create_team.assert_called_once()
        submitted = [call.args[1] for call in client.create_task.call_args_list]
        assert [task.subject for task in submitted] == ["T1", "T2"]
        assert {task.assign_to for task in submitted} == {"worker"}

# ---------------------------------------------------------------------------
# Tests: teardown
# ---------------------------------------------------------------------------


class TestTeardown:
    @pytest.mark.asyncio
    async def test_teardown_on_completion_deletes_agents(self) -> None:
        client = make_mock_client()
        client.create_agent = AsyncMock(return_value="agent-to-delete")
        spec = make_executor_spec(teardown="on_completion")

        executor = WorkflowExecutor(client, poll_interval=0.01)
        await executor.execute(spec)

        client.delete_agent.assert_called_once_with("agent-to-delete")

    @pytest.mark.asyncio
    async def test_teardown_never_skips_deletion(self) -> None:
        client = make_mock_client()
        spec = make_executor_spec(teardown="never")

        executor = WorkflowExecutor(client, poll_interval=0.01)
        await executor.execute(spec)

        client.delete_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_teardown_on_failure_skips_on_success(self) -> None:
        client = make_mock_client()
        spec = make_executor_spec(teardown="on_failure")

        executor = WorkflowExecutor(client, poll_interval=0.01)
        state = await executor.execute(spec)

        assert state.status == "completed"
        client.delete_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_completed_state_on_success(self) -> None:
        client = make_mock_client()
        spec = make_executor_spec()

        executor = WorkflowExecutor(client, poll_interval=0.01)
        state = await executor.execute(spec)

        assert state.status == "completed"
        assert state.completed_at is not None

    @pytest.mark.asyncio
    async def test_failed_state_when_task_fails(self) -> None:
        client = make_mock_client()
        client.get_tasks = AsyncMock(return_value=[{"subject": "Task 1", "status": "failed"}])
        spec = make_executor_spec(teardown="never")

        executor = WorkflowExecutor(client, poll_interval=0.01)
        state = await executor.execute(spec)

        assert state.status == "failed"
        assert state.error is not None


# ---------------------------------------------------------------------------
# Tests: error paths
# ---------------------------------------------------------------------------


class TestErrorPaths:
    @pytest.mark.asyncio
    async def test_teardown_runs_even_when_execution_fails(self) -> None:
        """Teardown (delete_agent) must run even when the workflow raises mid-execution."""
        client = make_mock_client()
        client.create_agent = AsyncMock(return_value="agent-to-teardown")
        spec = make_executor_spec(teardown="on_failure")

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
        client = make_mock_client()

        # Agent 1 succeeds, agent 2 raises AgamemnonError
        client.create_agent = AsyncMock(
            side_effect=[
                "agent-id-first",
                AgamemnonError(500, "internal server error"),
            ]
        )

        spec = make_executor_spec(
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
        client = make_mock_client()
        client.create_agent = AsyncMock(return_value="agent-created-but-not-woken")
        client.wake_agent = AsyncMock(side_effect=AgamemnonError(503, "service unavailable"))

        spec = make_executor_spec(
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
        client = make_mock_client()
        client.create_agent = AsyncMock(
            side_effect=[
                "id-A",
                AgamemnonError(500, "boom"),
                "id-C",
            ]
        )

        spec = make_executor_spec(
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
        client = make_mock_client()
        executor = WorkflowExecutor(client, poll_interval=0.01)
        with pytest.raises(ValueError, match="Unknown hook event"):
            executor.add_hook("on_unknown_event", lambda **_: None)

    def test_add_hook_accepts_known_events(self) -> None:
        client = make_mock_client()
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
        client = make_mock_client()
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
    async def test_emitted_subjects_do_not_leak_across_execute_calls(
        self,
    ) -> None:
        """Reusing one executor for two workflows must re-emit on_task_complete
        for a repeated task subject — the event-driven monitor's per-instance
        _emitted_task_events set is reset at the top of each execute() (#162/#203)."""
        client = make_mock_client()
        # Reconcile seeds "Task 1" as completed for both runs.
        client.get_tasks = AsyncMock(return_value=[{"subject": "Task 1", "status": "completed"}])

        executor = WorkflowExecutor(client, poll_interval=0.01)
        calls: list[str] = []
        executor.add_hook("on_task_complete", lambda **kw: calls.append(kw["task"]["subject"]))

        spec = make_executor_spec()
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
        client = make_mock_client()
        spec = make_executor_spec()
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

        client = make_mock_client()
        spec = make_executor_spec(teardown="never")

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

        spec = make_executor_spec()
        existing_key = make_key("test-wf", "worker")
        client = make_mock_client()
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

        spec = make_executor_spec()
        team_key = make_key("test-wf", "team-a")
        client = make_mock_client()
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

        spec = make_executor_spec()
        existing_key = make_key("test-wf", "worker")
        client = make_mock_client()
        client.list_agents = AsyncMock(return_value=[{"id": "old-agent", "name": existing_key}])
        executor = WorkflowExecutor(client, poll_interval=0.01, force=True)
        state = await executor.execute(spec)
        client.create_agent.assert_called_once()
        assert state.created_agents["worker"] == "agent-id-001"

    @pytest.mark.asyncio
    async def test_partial_prior_run_completes_missing_resources(self) -> None:
        from telemachy.idempotency import make_key

        spec = make_executor_spec()
        client = make_mock_client()
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

        spec = make_executor_spec()
        client = make_mock_client()
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

        spec = make_executor_spec(teardown="never")
        client = make_mock_client()
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
    spec = make_executor_spec(
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
    client = make_mock_client()
    executor = WorkflowExecutor(client, poll_interval=0.01)
    spec = make_executor_spec()
    result = await executor.execute(spec, workflow_id="custom-42")
    assert result.workflow_id == "custom-42"


# === Observability tests ===


@pytest.mark.asyncio
async def test_workflow_id_propagates_to_logs(caplog: pytest.LogCaptureFixture) -> None:
    """workflow_id from context is attached to every log record by filter."""
    from telemachy.telemetry import WorkflowContextLogFilter

    client = make_mock_client()
    spec = make_executor_spec()
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

    client = make_mock_client()
    spec = make_executor_spec()
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

    client = make_mock_client()
    spec = make_executor_spec()

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


# ---------------------------------------------------------------------------
# Tests: NATS event-driven monitoring
# ---------------------------------------------------------------------------


class TestNatsMonitoring:
    @pytest.mark.asyncio
    async def test_reconcile_seeds_terminal_status_without_polling(self) -> None:
        """The post-subscribe snapshot seeds an already-terminal task.

        The bounded ``get_tasks`` count is the structural guard: one reconcile
        snapshot plus at most one idempotent reuse check per team, never the
        dozens of calls a status-polling loop would issue.
        """
        client = make_mock_client()
        spec = make_executor_spec(
            tasks=[{"subject": "Task 1", "description": "...", "assign_to": "worker"}]
        )
        client.get_tasks.return_value = [{"subject": "Task 1", "status": "completed"}]
        monitor = make_mock_monitor()

        with patch("telemachy.executor.NatsMonitor", return_value=monitor):
            state = await WorkflowExecutor(client, poll_interval=0.01).execute(spec)

        assert state.status == "completed"
        assert monitor.latest_status("Task 1") == "completed"
        assert client.get_tasks.call_count <= 2

    @pytest.mark.asyncio
    async def test_dependent_task_waits_for_predecessor_terminal_event(self) -> None:
        """A blocked task is submitted only after its predecessor is terminal."""
        client = make_mock_client()
        spec = make_executor_spec(
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

        mock_monitor = make_mock_monitor()
        mock_monitor.latest_status = MagicMock(
            side_effect=lambda subj: {
                "Task 1": "completed",
                "Task 2": "completed",
            }.get(subj)
        )

        with patch("telemachy.executor.NatsMonitor", return_value=mock_monitor):
            state = await WorkflowExecutor(client, poll_interval=0.01).execute(spec)

        assert state.status == "completed"
        submitted = [call.args[1].subject for call in client.create_task.call_args_list]
        assert submitted == ["Task 1", "Task 2"]

    @pytest.mark.asyncio
    async def test_wait_for_all_terminal_skips_unsubmitted_tasks(self) -> None:
        """Test that monitor skips tasks that were never submitted."""

        client = make_mock_client()
        spec = make_executor_spec(
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

        mock_monitor = make_mock_monitor()
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

