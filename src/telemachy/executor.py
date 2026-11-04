"""WorkflowExecutor: orchestrates the full workflow lifecycle via ProjectAgamemnon."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from telemachy.agamemnon_client import AgamemnonClient, AgamemnonError
from telemachy.config import settings
from telemachy.constants import DONE_STATUSES as _DONE_STATUSES
from telemachy.models import AgentSpec, TeamSpec, WorkflowSpec, WorkflowState
from telemachy.nats_monitor import NatsMonitor, NatsUnavailableError
from telemachy.telemetry import (
    get_tracer,
    workflow_id_var,
    workflow_name_var,
)

logger = logging.getLogger(__name__)


class WorkflowTimeoutError(Exception):
    """Raised when workflow monitoring exceeds the configured timeout or max poll count."""


class WorkflowConnectivityError(Exception):
    """Raised when the event bus (NATS) connection is lost mid-workflow."""


class WorkflowExecutor:
    """Executes a WorkflowSpec against Agamemnon, monitoring until completion.

    Completion monitoring is event-driven: the executor subscribes to Agamemnon's
    NATS task-lifecycle subjects and waits on per-task terminal events rather than
    polling Agamemnon over HTTP (#3). NATS is a hard runtime dependency — the
    monitoring phase fails fast (NatsUnavailableError) if the broker is
    unreachable or the connection drops mid-workflow.
    """

    def __init__(
        self,
        client: AgamemnonClient,
        poll_interval: float = 5.0,
        dry_run: bool = False,
        stop_event: asyncio.Event | None = None,
        max_concurrent_provisioning: int = 16,
        force: bool = False,
        existing_snapshot: tuple[list[dict[str, object]], list[dict[str, object]]] | None = None,
    ) -> None:
        self._client = client
        self._poll_interval = poll_interval
        self._dry_run = dry_run
        self._stop_event = stop_event
        # Bound concurrent agent-provisioning fan-out so a workflow with many
        # agents does not overwhelm Agamemnon (#166). Default 16 matches
        # typical small-fleet sizing; callers can raise/lower as needed.
        self._provision_semaphore = asyncio.Semaphore(max(1, max_concurrent_provisioning))
        self._force = force
        # If the caller already fetched list_agents()/list_teams() (e.g. cli.run for
        # the --force warning), reuse that snapshot to avoid a second API round-trip.
        self._injected_snapshot = existing_snapshot
        self._existing_agents_by_key: dict[str, str] = {}
        self._existing_teams_by_key: dict[str, str] = {}
        self._hooks: dict[str, list[Callable[..., Any]]] = {
            "on_task_complete": [],
            "on_task_failed": [],
            "on_workflow_complete": [],
            "on_workflow_failed": [],
        }
        # Subjects for which a terminal-state callback has already been emitted.
        # Declared here (rather than via getattr/setattr) so the attribute is
        # statically typed and per-instance; reset at the top of each execute().
        self._emitted_task_events: set[str] = set()

    def add_hook(self, event: str, callback: Callable[..., Any]) -> None:
        """Register a callback for a workflow execution event.

        Supported events: on_task_complete, on_task_failed,
        on_workflow_complete, on_workflow_failed.
        """
        if event not in self._hooks:
            raise ValueError(
                f"Unknown hook event {event!r}. "
                f"Valid events: {sorted(self._hooks)}"
            )
        self._hooks[event].append(callback)

    async def _emit(self, event: str, **kwargs: Any) -> None:
        """Fire all callbacks registered for *event*."""
        for cb in self._hooks.get(event, []):
            if inspect.iscoroutinefunction(cb):
                await cb(**kwargs)
            else:
                cb(**kwargs)

    async def execute(self, spec: WorkflowSpec, workflow_id: str | None = None) -> WorkflowState:
        """Run a full workflow: provision → assign tasks → monitor → teardown."""
        # Reset per-execution state so reusing the same executor for a second
        # workflow does not leak emitted-event subjects from the prior run (#203).
        self._emitted_task_events = set()
        timeout = (
            spec.timeout_seconds
            if spec.timeout_seconds is not None
            else settings.default_workflow_timeout
        )
        try:
            return await asyncio.wait_for(self._run(spec, workflow_id), timeout=timeout)
        except TimeoutError as exc:
            raise WorkflowTimeoutError(
                f"Workflow '{spec.name}' exceeded its execution timeout of {timeout}s"
            ) from exc

    async def _run(self, spec: WorkflowSpec, workflow_id: str | None = None) -> WorkflowState:
        """Internal execution body — wrapped by execute() with a timeout."""
        workflow_id = workflow_id or str(uuid.uuid4())[:8]
        wf_token = workflow_id_var.set(workflow_id)
        name_token = workflow_name_var.set(spec.name)
        try:
            with get_tracer().start_as_current_span(
                "telemachy.workflow",
                attributes={
                    "telemachy.workflow_id": workflow_id,
                    "telemachy.workflow_name": spec.name,
                    "telemachy.agent_count": len(spec.agents),
                    "telemachy.team_count": len(spec.teams),
                },
            ):
                state = WorkflowState(
                    workflow_id=workflow_id,
                    spec=spec,
                    status="pending",
                    started_at=_now(),
                )
                logger.info("Starting workflow '%s' (id=%s)", spec.name, workflow_id)

                try:
                    state.status = "running"

                    # Populate idempotency lookup tables (skipped on dry-run or force).
                    if not self._dry_run and not self._force:
                        if self._injected_snapshot is not None:
                            agents_snapshot, teams_snapshot = self._injected_snapshot
                        else:
                            agents_snapshot = await self._client.list_agents()
                            teams_snapshot = await self._client.list_teams()
                        self._existing_agents_by_key = {
                            str(a.get("name", "")): str(a.get("id", ""))
                            for a in agents_snapshot
                            if a.get("name") and a.get("id")
                        }
                        self._existing_teams_by_key = {
                            str(t.get("name", "")): str(t.get("id", ""))
                            for t in teams_snapshot
                            if t.get("name") and t.get("id")
                        }

                    # Provision all agents concurrently. _provision_agents aliases its
                    # internal id_map to state.created_agents up front so teardown sees
                    # every successfully-created agent even on partial failure (#164).
                    await self._provision_agents(spec.agents, spec.name, state)

                    # Create teams FIRST (no tasks yet) so we can subscribe to their
                    # NATS task-lifecycle subjects before submitting any task — this
                    # closes the create→subscribe race (#3).
                    state.created_teams = await self._create_teams_only(
                        spec.teams, state.created_agents, spec.name
                    )

                    # Monitor until all tasks reach a terminal state (skipped in dry-run).
                    if self._dry_run:
                        logger.info("[dry-run] Skipping monitoring — no real tasks submitted")
                    else:
                        async with NatsMonitor(
                            settings.nats_url, stop_event=self._stop_event
                        ) as monitor:
                            for team_id in state.created_teams.values():
                                await monitor.subscribe_team(team_id)
                            await self._reconcile_initial(state, monitor)
                            await self._submit_all_team_tasks(
                                spec.teams,
                                state.created_agents,
                                state.created_teams,
                                state,
                                monitor,
                            )
                            await self._wait_for_all_terminal(state, monitor)

                    if state.status == "cancelled":
                        # Graceful stop-event cancellation — monitor returned early.
                        state.completed_at = _now()
                        logger.warning("Workflow '%s' was cancelled via stop event", spec.name)
                    else:
                        state.status = "completed"
                        state.completed_at = _now()
                        logger.info("Workflow '%s' completed successfully", spec.name)
                        await self._emit("on_workflow_complete", state=state)

                except asyncio.CancelledError:
                    state.status = "cancelled"
                    state.completed_at = _now()
                    logger.warning("Workflow '%s' was cancelled", spec.name)
                    raise

                except Exception as exc:
                    state.status = "failed"
                    state.completed_at = _now()
                    state.error = str(exc)
                    # A lost event-bus connection should still honour an on_completion
                    # teardown policy (mirrors the prior connectivity-loss semantics, #161).
                    if isinstance(exc, NatsUnavailableError):
                        state.connectivity_failed = True
                    logger.error("Workflow '%s' failed: %s", spec.name, exc)
                    await self._emit("on_workflow_failed", state=state, error=exc)

                finally:
                    await self._teardown(state)

                return state
        finally:
            workflow_id_var.reset(wf_token)
            workflow_name_var.reset(name_token)

    # === Provisioning ===

    async def _provision_agents(
        self,
        agents: list[AgentSpec],
        workflow_name: str,
        state: WorkflowState,
    ) -> dict[str, str]:
        """Create all agents concurrently. Returns {agent_name: agamemnon_id}.

        The returned dict is also aliased to state.created_agents up front and
        mutated incrementally: each per-agent coroutine records its id the moment
        create_agent returns, BEFORE wake_agent runs, so teardown sees every
        created agent even if wake_agent or a sibling coroutine fails (#164).
        """
        with get_tracer().start_as_current_span(
            "telemachy.provision_agents", attributes={"telemachy.agent_count": len(agents)}
        ):
            logger.info("Provisioning %d agent(s)...", len(agents))

            # Shared id map — mutated by _provision_one_agent the instant each
            # create_agent call returns. Aliased to state.created_agents so teardown
            # sees newly-created agents even if the workflow raises mid-fan-out (#164).
            id_map: dict[str, str] = {}
            state.created_agents = id_map

            async def _bounded(agent: AgentSpec) -> tuple[str, str]:
                async with self._provision_semaphore:
                    return await self._provision_one_agent(agent, workflow_name, id_map)

            coros = [_bounded(agent) for agent in agents]
            raw_results: list[tuple[str, str] | BaseException] = await asyncio.gather(
                *coros, return_exceptions=True
            )
            first_exc: BaseException | None = next(
                (r for r in raw_results if isinstance(r, BaseException)), None
            )
            if first_exc is not None:
                raise first_exc
            logger.info("All agents provisioned: %s", id_map)
            return id_map

    async def _provision_one_agent(
        self,
        spec: AgentSpec,
        workflow_name: str,
        id_map: dict[str, str],
    ) -> tuple[str, str]:
        """Create a single agent and wake it. Returns (name, agamemnon_id).

        If an agent with this workflow's idempotency key already exists, reuse it.

        Mutates *id_map* as a side effect: the Agamemnon id is recorded the
        moment create_agent (or reuse) resolves — BEFORE wake_agent is awaited —
        so a wake failure does not leave an orphan agent untracked by teardown
        (#164). This is safe under asyncio's cooperative single-threaded
        scheduling: the assignment is between two await points and cannot
        interleave with other coroutines. Agent-name collisions are
        prevented by WorkflowSpec.validate_unique_agent_names.
        """
        from telemachy.idempotency import make_key

        if self._dry_run:
            dry_id = f"dry-run-agent-{spec.name}"
            id_map[spec.name] = dry_id
            logger.info("[dry-run] Would create agent '%s' → id=%s", spec.name, dry_id)
            return spec.name, dry_id

        key = make_key(workflow_name, spec.name)
        existing_id = self._existing_agents_by_key.get(key)
        if existing_id and not self._force:
            # Record the reused id BEFORE waking it, so a wake failure does not
            # leave the (reused) agent untracked by teardown (#164).
            id_map[spec.name] = existing_id
            logger.info(
                "Reusing existing agent '%s' (id=%s, key=%s)",
                spec.name,
                existing_id,
                key,
            )
            # The reused agent may already be running. wake_agent maps to
            # POST /v1/agents/{id}/start; tolerate already-running responses by
            # swallowing only conflict-shaped AgamemnonError (status 409, or 400
            # with a recognisable message). Anything else re-raises.
            try:
                await self._client.wake_agent(existing_id)
            except AgamemnonError as exc:
                already_running = exc.status_code == 409 or (
                    exc.status_code == 400 and "running" in str(exc).lower()
                )
                if not already_running:
                    raise
                logger.info(
                    "Agent '%s' (id=%s) was already running; reuse continues",
                    spec.name,
                    existing_id,
                )
            return spec.name, existing_id

        agent_id = await self._client.create_agent(spec, idempotency_name=key)
        # Record the id BEFORE any further awaitable that could fail (#164).
        id_map[spec.name] = agent_id
        logger.debug("Created agent '%s' → id=%s (key=%s)", spec.name, agent_id, key)
        await self._client.wake_agent(agent_id)
        logger.debug("Woke agent '%s' (id=%s)", spec.name, agent_id)
        return spec.name, agent_id

    # === Team and task creation ===

    async def _create_teams_only(
        self,
        teams: list[TeamSpec],
        agent_ids: dict[str, str],
        workflow_name: str,
    ) -> dict[str, str]:
        """Create all teams concurrently WITHOUT submitting tasks yet.

        Tasks are submitted later by _submit_all_team_tasks, after the NATS
        monitor has subscribed to each team's task-lifecycle subjects — this
        closes the create→subscribe race so no terminal event is missed (#3).
        Idempotency reuse of existing teams is preserved (#55).
        Returns {team_name: team_id}.
        """
        with get_tracer().start_as_current_span(
            "telemachy.create_teams", attributes={"telemachy.team_count": len(teams)}
        ):
            results: list[tuple[str, str]] = await asyncio.gather(
                *[self._create_team_only(team_spec, agent_ids, workflow_name) for team_spec in teams]
            )
            return dict(results)

    async def _create_team_only(
        self,
        team_spec: TeamSpec,
        agent_ids: dict[str, str],
        workflow_name: str,
    ) -> tuple[str, str]:
        """Create (or reuse) a single team; do NOT submit its tasks yet."""
        from telemachy.idempotency import make_key

        if self._dry_run:
            dry_id = f"dry-run-team-{team_spec.name}"
            logger.info("[dry-run] Would create team '%s' → id=%s", team_spec.name, dry_id)
            for task_spec in team_spec.tasks:
                logger.info(
                    "[dry-run] Would submit task '%s' (assign_to=%s, blocked_by=%s)",
                    task_spec.subject,
                    task_spec.assign_to,
                    task_spec.blocked_by,
                )
            return team_spec.name, dry_id

        member_ids = [agent_ids[name] for name in team_spec.agents]
        key = make_key(workflow_name, team_spec.name)
        existing_id = self._existing_teams_by_key.get(key)
        if existing_id and not self._force:
            logger.info(
                "Reusing existing team '%s' (id=%s, key=%s); membership not reconciled",
                team_spec.name,
                existing_id,
                key,
            )
            team_id = existing_id
        else:
            team_id = await self._client.create_team(key, member_ids)
            logger.info("Created team '%s' → id=%s (key=%s)", team_spec.name, team_id, key)
        return team_spec.name, team_id

    async def _submit_all_team_tasks(
        self,
        teams: list[TeamSpec],
        agent_ids: dict[str, str],
        team_ids: dict[str, str],
        state: WorkflowState,
        monitor: NatsMonitor,
    ) -> None:
        """Submit tasks for all teams concurrently; respect dependencies via monitor."""
        await asyncio.gather(
            *[
                self._submit_tasks_with_deps(team_ids[ts.name], ts, agent_ids, state, monitor)
                for ts in teams
            ]
        )

    async def _submit_tasks_with_deps(
        self,
        team_id: str,
        team_spec: TeamSpec,
        agent_ids: dict[str, str],
        state: WorkflowState,
        monitor: NatsMonitor,
    ) -> None:
        """Submit tasks in dependency order, using NATS terminal events to unblock.

        Cross-team and intra-team dependencies are awaited on the monitor's
        per-subject terminal events (no HTTP polling, no asyncio.sleep). If a
        dependency has failed/errored/cancelled, the dependent task is skipped
        rather than waiting forever (prevents infinite loop — see #13).

        Idempotency reuse of already-submitted tasks is preserved (#55): on a
        non-force run, existing tasks for this team are reused and their initial
        status is seeded into the monitor so completed predecessors unblock
        immediately.
        """
        submitted: dict[str, str] = {}   # subject → task_id
        completed_subjects: set[str] = set()
        failed_subjects: set[str] = set()
        skipped_subjects: set[str] = set()

        if not self._force:
            existing = await self._client.get_tasks(team_id)
            for t in existing:
                subj = str(t.get("subject", ""))
                tid = str(t.get("id", ""))
                status = str(t.get("status", ""))
                if subj and tid:
                    submitted[subj] = tid
                    # Seed the monitor so reused-task terminal status unblocks deps.
                    if status:
                        monitor.record_status(subj, status)
                    if status == "completed":
                        completed_subjects.add(subj)
                    elif status in {"failed", "error", "cancelled"}:
                        failed_subjects.add(subj)
            if submitted:
                # Reused tasks count toward the watched set so monitoring waits on them.
                state.submitted_task_subjects.update(submitted)
                logger.info(
                    "Reusing %d existing task(s) in team %s", len(submitted), team_spec.name
                )

        pending = [t for t in team_spec.tasks if t.subject not in submitted]

        while pending:
            # Skip tasks whose dependencies have failed/were-skipped (preserves #13).
            newly_skipped = [
                t for t in pending
                if any(dep in failed_subjects or dep in skipped_subjects for dep in t.blocked_by)
            ]
            for task_spec in newly_skipped:
                logger.warning(
                    "Skipping task '%s': a dependency failed or was skipped (%s)",
                    task_spec.subject,
                    [
                        dep
                        for dep in task_spec.blocked_by
                        if dep in failed_subjects or dep in skipped_subjects
                    ],
                )
                skipped_subjects.add(task_spec.subject)
                pending.remove(task_spec)

            ready = [
                t for t in pending
                if all(dep in completed_subjects for dep in t.blocked_by)
            ]
            if not ready:
                if not pending:
                    break
                if self._stop_event and self._stop_event.is_set():
                    logger.warning("Stop event set — aborting task submission")
                    raise asyncio.CancelledError("Task submission cancelled by stop event")

                # Wait for ANY signal that could unblock us:
                #   - a predecessor's terminal_event fires (cross-team predecessor done)
                #   - submitted_event fires (another team submitted a task we depend on)
                #   - 30s watchdog wake (re-check stop_event)
                pred_subjects = {d for t in pending for d in t.blocked_by}
                waiters: list[asyncio.Task[Any]] = [
                    asyncio.create_task(monitor.terminal_event(s).wait()) for s in pred_subjects
                ]
                waiters.append(asyncio.create_task(monitor.submitted_event.wait()))
                try:
                    await asyncio.wait(
                        waiters,
                        return_when=asyncio.FIRST_COMPLETED,
                        timeout=30.0,
                    )
                finally:
                    for w in waiters:
                        if not w.done():
                            w.cancel()

                # Re-derive status sets from the monotonic latest_status snapshot.
                for subj in list(submitted) + list(pred_subjects):
                    s = monitor.latest_status(subj)
                    if s == "completed":
                        completed_subjects.add(subj)
                    elif s in {"failed", "error", "cancelled"}:
                        failed_subjects.add(subj)
                continue

            for task_spec in ready:
                blocked_by_ids = [
                    submitted[dep] for dep in task_spec.blocked_by if dep in submitted
                ]
                # Resolve agent name → Agamemnon agent ID before submitting (#12)
                resolved_agent_id: str | None = None
                if task_spec.assign_to:
                    resolved_agent_id = agent_ids.get(task_spec.assign_to)
                task_id = await self._client.create_task(
                    team_id,
                    task_spec,
                    blocked_by_ids,
                    assignee_agent_id=resolved_agent_id,
                )
                submitted[task_spec.subject] = task_id
                state.submitted_task_subjects.add(task_spec.subject)
                monitor.notify_submitted()
                logger.info("Submitted task '%s' → id=%s", task_spec.subject, task_id)
                pending.remove(task_spec)

    # === Monitoring (NATS event-driven) ===

    async def _reconcile_initial(self, state: WorkflowState, monitor: NatsMonitor) -> None:
        """One-shot snapshot after subscription to close the create→subscribe race.

        Events arriving between snapshot REQUEST and snapshot RESPONSE are buffered into
        monitor's per-subject Event; record_status' terminal-sticky rule prevents the
        snapshot from clobbering an already-terminal status.
        """
        for team_id in state.created_teams.values():
            for task in await self._client.get_tasks(team_id):
                subj = str(task.get("subject", ""))
                status = str(task.get("status", ""))
                if subj and status:
                    monitor.record_status(subj, status)

    async def _wait_for_all_terminal(self, state: WorkflowState, monitor: NatsMonitor) -> None:
        """Wait until every SUBMITTED task subject has a terminal status via NATS events.

        Respects stop_event and monitor_timeout. The set of subjects
        for which we've emitted a terminal-state callback is the per-execution
        instance set (#162/#203). NATS connection loss raises NatsUnavailableError.
        """
        with get_tracer().start_as_current_span("telemachy.monitor_completion"):
            logger.info("Monitoring workflow '%s' via NATS events", state.spec.name)
            start = time.monotonic()
            timeout = settings.monitor_timeout_seconds
            any_failed = False
            while True:
                if self._stop_event and self._stop_event.is_set():
                    logger.warning(
                        "Stop event set — aborting monitoring for workflow '%s'",
                        state.spec.name,
                    )
                    state.status = "cancelled"
                    return
                if not monitor.connected:
                    state.connectivity_failed = True
                    raise NatsUnavailableError(
                        f"NATS connection lost mid-workflow for '{state.spec.name}'"
                    )
                if time.monotonic() - start > timeout:
                    raise WorkflowTimeoutError(
                        f"Monitoring timed out after {timeout}s for '{state.spec.name}'"
                    )

                # Snapshot of subjects we are actually waiting on (Decision 6).
                watched = set(state.submitted_task_subjects)

                for subj in watched:
                    status = monitor.latest_status(subj)
                    if status in {"failed", "error"} and subj not in self._emitted_task_events:
                        self._emitted_task_events.add(subj)
                        any_failed = True
                        logger.warning("Task '%s' failed (status=%s)", subj, status)
                        await self._emit(
                            "on_task_failed", task={"subject": subj, "status": status}, team=""
                        )
                    elif status == "completed" and subj not in self._emitted_task_events:
                        self._emitted_task_events.add(subj)
                        await self._emit(
                            "on_task_complete", task={"subject": subj, "status": status}, team=""
                        )

                unfinished = [
                    s for s in watched if monitor.latest_status(s) not in _DONE_STATUSES
                ]
                if not unfinished:
                    if any_failed:
                        raise RuntimeError("One or more tasks failed during workflow execution")
                    return

                # Wake on any terminal event OR 5s watchdog (keeps stop_event/timeout responsive).
                waiters = [
                    asyncio.create_task(monitor.terminal_event(s).wait()) for s in unfinished
                ]
                try:
                    await asyncio.wait(
                        waiters, return_when=asyncio.FIRST_COMPLETED, timeout=5.0
                    )
                finally:
                    for w in waiters:
                        if not w.done():
                            w.cancel()

    # === Teardown ===

    async def _teardown(self, state: WorkflowState) -> None:
        """Delete agents and teams based on the workflow's teardown policy."""
        with get_tracer().start_as_current_span(
            "telemachy.teardown",
            attributes={"telemachy.policy": state.spec.teardown, "telemachy.status": state.status},
        ):
            if self._dry_run:
                logger.info("[dry-run] Skipping teardown")
                return

            policy = state.spec.teardown

            # A connectivity-induced failure (NATS bus lost mid-run) should still
            # honour an `on_completion` policy — the workflow author asked us to
            # clean up after this workflow, and "the event bus went away mid-run"
            # should not leak agents and teams (see #161). We do NOT extend
            # on_completion to *task* failures, which are the existing
            # "leave for inspection" behaviour.
            should_teardown = (
                (policy == "on_completion" and state.status == "completed")
                or (policy == "on_completion" and state.connectivity_failed)
                or (policy == "on_failure" and state.status == "failed")
            )

            if not should_teardown:
                logger.info(
                    "Teardown skipped (policy=%s, status=%s)", policy, state.status
                )
                return

            logger.info("Running teardown for workflow '%s'...", state.spec.name)

            for name, team_id in state.created_teams.items():
                try:
                    await self._client.delete_team(team_id)
                    logger.debug("Deleted team '%s' (id=%s)", name, team_id)
                except AgamemnonError as exc:
                    logger.warning("Failed to delete team '%s': %s", name, exc)

            for name, agent_id in state.created_agents.items():
                try:
                    await self._client.delete_agent(agent_id)
                    logger.debug("Deleted agent '%s' (id=%s)", name, agent_id)
                except AgamemnonError as exc:
                    logger.warning("Failed to delete agent '%s': %s", name, exc)

            logger.info("Teardown complete")


def _now() -> str:
    return datetime.now(tz=UTC).isoformat()


async def run_workflow(
    spec: WorkflowSpec,
    dry_run: bool = False,
    stop_event: asyncio.Event | None = None,
    force: bool = False,
) -> WorkflowState:
    """Convenience function: create a client from settings and execute a workflow."""
    async with AgamemnonClient(**settings.client_kwargs()) as client:
        executor = WorkflowExecutor(
            client,
            dry_run=dry_run,
            stop_event=stop_event,
            force=force,
        )
        return await executor.execute(spec)
