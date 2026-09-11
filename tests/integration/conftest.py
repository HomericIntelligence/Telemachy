"""Integration test fixtures: stateful mock Agamemnon HTTP server (respx)."""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest_asyncio
import respx

from telemachy.agamemnon_client import AgamemnonClient
from telemachy.models import WorkflowSpec

_TEAM_TASKS_RE = re.compile(r"^/v1/teams/(?P<tid>[^/]+)/tasks$")
_TEAM_TASK_RE = re.compile(r"^/v1/teams/(?P<tid>[^/]+)/tasks/(?P<task_id>[^/]+)$")
_TEAM_MEMBERS_RE = re.compile(r"^/v1/teams/(?P<tid>[^/]+)$")
_AGENT_DELETE_RE = re.compile(r"^/v1/agents/(?P<aid>[^/]+)$")


@dataclass
class MockAgamemnonState:
    """In-memory state plus explicit fault-injection knobs."""

    agents: dict[str, dict[str, Any]] = field(default_factory=dict)
    teams: dict[str, dict[str, Any]] = field(default_factory=dict)
    tasks: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    # Per-subject sequence of statuses advanced one step per GET tasks poll.
    # Default for newly-created tasks is "pending" — tests MUST script a
    # terminal status or call set_all_tasks_complete() to let the monitor exit.
    task_status_script: dict[str, list[str]] = field(default_factory=dict)
    # Permanent override (every call returns this code) — None means use queue.
    permanent_create_agent_status: int | None = None
    # FIFO queue of statuses for POST /v1/agents; empty queue ⇒ 201.
    create_agent_status_queue: list[int] = field(default_factory=list)
    # FIFO queue of statuses for GET /v1/teams/{id}/tasks; empty queue ⇒ 200.
    get_tasks_status_queue: list[int] = field(default_factory=list)
    # If set, POST /v1/agents raises this exception type instead of responding.
    create_agent_raise: type[BaseException] | None = None
    _next_id: int = 0

    def new_id(self, prefix: str) -> str:
        self._next_id += 1
        return f"{prefix}-{self._next_id:04d}"

    def set_all_tasks_complete(self) -> None:
        """Mark every existing task as completed on the next GET poll."""
        for team_tasks in self.tasks.values():
            for t in team_tasks:
                self.task_status_script.setdefault(t["subject"], []).append("completed")


def _read_json(request: httpx.Request) -> dict[str, Any]:
    body = request.read()
    return json.loads(body) if body else {}


def _install_routes(router: respx.MockRouter, state: MockAgamemnonState) -> None:
    def create_agent(request: httpx.Request) -> httpx.Response:
        if state.create_agent_raise is not None:
            raise state.create_agent_raise("injected fault")
        if state.permanent_create_agent_status is not None:
            return httpx.Response(state.permanent_create_agent_status, json={"detail": "boom"})
        if state.create_agent_status_queue:
            code = state.create_agent_status_queue.pop(0)
            if code >= 400:
                return httpx.Response(code, json={"detail": f"status {code}"})
        body = _read_json(request)
        aid = state.new_id("agent")
        state.agents[aid] = {"id": aid, **body}
        return httpx.Response(201, json={"agent": {"id": aid}})

    def create_docker_agent(request: httpx.Request) -> httpx.Response:
        body = _read_json(request)
        aid = state.new_id("docker-agent")
        state.agents[aid] = {"id": aid, **body}
        return httpx.Response(201, json={"agent": {"id": aid}})

    def start_agent(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    def stop_agent(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    def delete_agent(request: httpx.Request) -> httpx.Response:
        m = _AGENT_DELETE_RE.match(request.url.path)
        if m:
            state.agents.pop(m.group("aid"), None)
        return httpx.Response(204)

    def list_agents(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"agents": list(state.agents.values())})

    def list_teams(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"teams": list(state.teams.values())})

    def create_team(request: httpx.Request) -> httpx.Response:
        body = _read_json(request)
        tid = state.new_id("team")
        state.teams[tid] = {"id": tid, "name": body.get("name", ""), "agentIds": []}
        state.tasks[tid] = []
        return httpx.Response(201, json={"team": {"id": tid}})

    def set_team_members(request: httpx.Request) -> httpx.Response:
        m = _TEAM_MEMBERS_RE.match(request.url.path)
        body = _read_json(request)
        if m and m.group("tid") in state.teams:
            state.teams[m.group("tid")]["agentIds"] = body.get("agentIds", [])
        return httpx.Response(200, json={"team": state.teams.get(m.group("tid"), {}) if m else {}})

    def delete_team(request: httpx.Request) -> httpx.Response:
        m = _TEAM_MEMBERS_RE.match(request.url.path)
        if m:
            state.teams.pop(m.group("tid"), None)
            state.tasks.pop(m.group("tid"), None)
        return httpx.Response(204)

    def create_task(request: httpx.Request) -> httpx.Response:
        m = _TEAM_TASKS_RE.match(request.url.path)
        if not m:
            return httpx.Response(404, json={"detail": "no team"})
        tid = m.group("tid")
        body = _read_json(request)
        task_id = state.new_id("task")
        record = {
            "id": task_id,
            "subject": body["subject"],
            "description": body.get("description", ""),
            # POLA: default to pending so a test that forgets to script status hangs loudly.
            "status": "pending",
            "assigneeAgentId": body.get("assigneeAgentId"),
            "blockedBy": body.get("blockedBy", []),
        }
        state.tasks.setdefault(tid, []).append(record)
        return httpx.Response(201, json={"task": {"id": task_id}})

    def update_task(request: httpx.Request) -> httpx.Response:
        m = _TEAM_TASK_RE.match(request.url.path)
        if not m:
            return httpx.Response(404, json={"detail": "no task"})
        body = _read_json(request)
        for t in state.tasks.get(m.group("tid"), []):
            if t["id"] == m.group("task_id"):
                t.update(body)
                return httpx.Response(200, json={"task": t})
        return httpx.Response(404, json={"detail": "no task"})

    def list_tasks(request: httpx.Request) -> httpx.Response:
        if state.get_tasks_status_queue:
            code = state.get_tasks_status_queue.pop(0)
            if code >= 400:
                return httpx.Response(code, json={"detail": f"status {code}"})
        m = _TEAM_TASKS_RE.match(request.url.path)
        if not m:
            return httpx.Response(404, json={"detail": "no team"})
        tid = m.group("tid")
        for t in state.tasks.get(tid, []):
            script = state.task_status_script.get(t["subject"])
            if script:
                t["status"] = script.pop(0)
        return httpx.Response(200, json={"tasks": state.tasks.get(tid, [])})

    # Register routes with explicit `name=` so tests can do router["create_agent"].calls.
    router.post("http://mock-agamemnon/v1/agents", name="create_agent").mock(side_effect=create_agent)
    router.post("http://mock-agamemnon/v1/agents/docker", name="create_docker_agent").mock(side_effect=create_docker_agent)
    router.post(url__regex=r"http://mock-agamemnon/v1/agents/[^/]+/start$", name="start_agent").mock(side_effect=start_agent)
    router.post(url__regex=r"http://mock-agamemnon/v1/agents/[^/]+/stop$", name="stop_agent").mock(side_effect=stop_agent)
    router.delete(url__regex=r"http://mock-agamemnon/v1/agents/[^/]+$", name="delete_agent").mock(side_effect=delete_agent)
    router.get("http://mock-agamemnon/v1/agents", name="list_agents").mock(side_effect=list_agents)
    router.get("http://mock-agamemnon/v1/teams", name="list_teams").mock(side_effect=list_teams)
    router.post("http://mock-agamemnon/v1/teams", name="create_team").mock(side_effect=create_team)
    router.put(url__regex=r"http://mock-agamemnon/v1/teams/[^/]+$", name="set_team_members").mock(side_effect=set_team_members)
    router.delete(url__regex=r"http://mock-agamemnon/v1/teams/[^/]+$", name="delete_team").mock(side_effect=delete_team)
    router.post(url__regex=r"http://mock-agamemnon/v1/teams/[^/]+/tasks$", name="create_task").mock(side_effect=create_task)
    router.put(url__regex=r"http://mock-agamemnon/v1/teams/[^/]+/tasks/[^/]+$", name="update_task").mock(side_effect=update_task)
    router.get(url__regex=r"http://mock-agamemnon/v1/teams/[^/]+/tasks$", name="list_tasks").mock(side_effect=list_tasks)


def make_spec(**overrides: Any) -> WorkflowSpec:
    """Shared workflow-spec builder for integration tests."""
    base: dict[str, Any] = {
        "apiVersion": "telemachy/v1",
        "metadata": {"name": "int-wf", "description": "integration"},
        "agents": [{"name": "worker", "runtime": "local"}],
        "teams": [
            {
                "name": "t1",
                "agents": ["worker"],
                "tasks": [{"subject": "do-it", "description": "x", "assign_to": "worker"}],
            }
        ],
        "teardown": "on_completion",
    }
    base.update(overrides)
    return WorkflowSpec.model_validate(base)


def payload_contains(actual: dict[str, Any], expected: dict[str, Any]) -> bool:
    """Return True iff every key in expected is present in actual with == value."""
    return all(actual.get(k) == v for k, v in expected.items())


@pytest_asyncio.fixture
async def mock_agamemnon() -> AsyncIterator[tuple[MockAgamemnonState, respx.MockRouter, AgamemnonClient]]:
    """Stateful mock + entered AgamemnonClient, both alive for the test body."""
    state = MockAgamemnonState()
    with respx.mock(base_url="http://mock-agamemnon", assert_all_called=False) as router:
        _install_routes(router, state)
        async with AgamemnonClient(
            url="http://mock-agamemnon",
            api_key="test-key",
            require_tls=False,
        ) as client:
            yield state, router, client
