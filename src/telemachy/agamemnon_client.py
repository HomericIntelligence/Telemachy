"""Async HTTP client wrapping the ProjectAgamemnon REST API."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from telemachy.models import AgentSpec, TaskSpec
from telemachy.rate_limiter import TokenBucket

logger = logging.getLogger(__name__)

class AgamemnonError(Exception):
    """Raised when the ProjectAgamemnon API returns an error response."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(f"Agamemnon API error {status_code}: {message}")


def _require(data: dict[str, Any], *keys: str, context: str = "") -> Any:
    """Safely traverse nested dict keys, raising AgamemnonError on missing keys."""
    val: Any = data
    for k in keys:
        if not isinstance(val, dict) or k not in val:
            ctx = f" in {context}" if context else ""
            raise AgamemnonError(
                0,
                f"Unexpected API response shape{ctx}: missing key {k!r}. Got: {val!r}",
            )
        val = val[k]
    return val


class AgamemnonClient:
    """Async client for ProjectAgamemnon REST API endpoints used by Telemachy."""

    def __init__(
        self,
        url: str,
        api_key: str = "",
        host_id: str = "hermes",
        require_tls: bool = False,
        nats_url: str = "",
        rate_limit_rps: float = 0.0,
        rate_limit_burst: int = 16,
    ) -> None:
        if require_tls:
            if not url.startswith("https://"):
                raise AgamemnonError(
                    0,
                    "TLS required (REQUIRE_TLS=true) but AGAMEMNON_URL uses plain HTTP. "
                    "Use https:// or set REQUIRE_TLS=false.",
                )
            if nats_url and not nats_url.startswith("tls://"):
                raise AgamemnonError(
                    0,
                    "TLS required (REQUIRE_TLS=true) but NATS_URL uses plain nats://. "
                    "Use tls:// or set REQUIRE_TLS=false.",
                )
        self._base_url = url.rstrip("/")
        self._host_id = host_id
        self._rate_limiter = TokenBucket(rate=rate_limit_rps, burst=rate_limit_burst)
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        else:
            # Empty AGAMEMNON_API_KEY silently disabled auth before #159 — log
            # so production misconfiguration is visible in the daemon log.
            logger.warning(
                "AgamemnonClient initialised with empty api_key — "
                "requests to %s will be UNAUTHENTICATED. Set AGAMEMNON_API_KEY "
                "for any non-local environment.",
                self._base_url,
            )
        self._headers = headers
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> AgamemnonClient:
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers,
            timeout=30.0,
        )
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    @property
    def _http(self) -> httpx.AsyncClient:
        if not self._client:
            raise RuntimeError("AgamemnonClient must be used as an async context manager")
        return self._client

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.is_error:
            try:
                detail = response.json().get("detail", response.text)
            except Exception:
                detail = response.text
            raise AgamemnonError(response.status_code, detail)

    async def _request_with_retry(
        self, method: str, url: str, *, max_attempts: int = 3, **kwargs: Any
    ) -> httpx.Response:
        """Issue an HTTP request, retrying on server errors and transient failures.

        Each attempt waits for the outbound rate-limit bucket (#160) before
        contacting Agamemnon. Default config (rate=0) is a no-op.
        Retry backoff is capped at 2.0s per `nats-publish-retry-backoff`.

        Retries on:
        - 5xx server errors
        - 429 Too Many Requests (rate-limit) — see #165
        - httpx ConnectError / TimeoutException
        """
        last_exc: Exception | None = None
        for attempt in range(max_attempts):
            await self._rate_limiter.acquire()
            try:
                resp = await self._http.request(method, url, **kwargs)
                if resp.status_code == 429:
                    last_exc = AgamemnonError(429, "Rate-limited (429); retrying")
                elif resp.status_code < 500:
                    return resp
                else:
                    last_exc = AgamemnonError(resp.status_code, f"Server error {resp.status_code}")
            except (httpx.ConnectError, httpx.TimeoutException) as e:
                last_exc = e
            if attempt < max_attempts - 1:
                # Exponential backoff, capped at 2.0s so a future increase of
                # max_attempts cannot produce unbounded per-retry sleeps that
                # would fight the outbound rate limiter (#160, on-theme).
                await asyncio.sleep(min(2**attempt, 2.0))
        raise AgamemnonError(0, f"Request failed after {max_attempts} attempts: {last_exc}")

    # === Agent endpoints ===

    async def create_agent(self, spec: AgentSpec, idempotency_name: str | None = None) -> str:
        """Create a local or docker agent. Returns the Agamemnon agent id."""
        if spec.runtime == "docker":
            return await self._create_docker_agent(spec, idempotency_name)
        return await self._create_local_agent(spec, idempotency_name)

    async def _create_local_agent(
        self, spec: AgentSpec, idempotency_name: str | None = None
    ) -> str:
        payload: dict[str, object] = {
            "name": idempotency_name or spec.name,
            "label": spec.name,
            "program": spec.program,
            "workingDirectory": spec.working_dir,
            "taskDescription": f"Telemachy-managed agent: {spec.name}",
        }
        if spec.model:
            payload["programArgs"] = f"--model {spec.model}"

        response = await self._request_with_retry("POST", "/v1/agents", json=payload)
        self._raise_for_status(response)
        return str(_require(response.json(), "agent", "id", context="create_agent"))

    async def _create_docker_agent(
        self, spec: AgentSpec, idempotency_name: str | None = None
    ) -> str:
        payload: dict[str, object] = {
            "name": idempotency_name or spec.name,
            "hostId": self._host_id,
            "image": spec.docker_image,
            "cpus": spec.cpus,
            "memory": spec.memory,
            "workingDirectory": spec.working_dir,
        }

        response = await self._request_with_retry("POST", "/v1/agents/docker", json=payload)
        self._raise_for_status(response)
        return str(_require(response.json(), "agent", "id", context="create_docker_agent"))

    async def wake_agent(self, agent_id: str) -> None:
        """Start a stopped agent."""
        response = await self._request_with_retry("POST", f"/v1/agents/{agent_id}/start")
        self._raise_for_status(response)

    async def hibernate_agent(self, agent_id: str) -> None:
        """Stop a running agent."""
        response = await self._request_with_retry("POST", f"/v1/agents/{agent_id}/stop")
        self._raise_for_status(response)

    async def delete_agent(self, agent_id: str) -> None:
        """Permanently delete an agent."""
        response = await self._request_with_retry("DELETE", f"/v1/agents/{agent_id}")
        self._raise_for_status(response)

    async def list_agents(self) -> list[dict[str, object]]:
        """List all agents."""
        response = await self._request_with_retry("GET", "/v1/agents")
        self._raise_for_status(response)
        agents: list[dict[str, object]] = response.json().get("agents", [])
        return agents

    async def list_teams(self) -> list[dict[str, object]]:
        """List all teams. Used to detect prior idempotent provisioning."""
        response = await self._request_with_retry("GET", "/v1/teams")
        self._raise_for_status(response)
        teams: list[dict[str, object]] = response.json().get("teams", [])
        return teams

    async def ping(self, timeout: float = 5.0) -> bool:
        """Single-shot liveness probe against ``GET /v1/agents``.

        Returns ``True`` iff Agamemnon answers a 2xx response within *timeout*
        seconds.

        NOTE: This method intentionally bypasses ``_request_with_retry``, so a
        probe costs at most *timeout* seconds. Wrapping ping() in 3 retries
        with exponential backoff would swallow up to ~7s of connectivity loss
        per probe and defeat the purpose of an out-of-band liveness signal.
        Callers that want retries or a failure threshold own that loop.
        """
        try:
            resp = await self._http.request("GET", "/v1/agents", timeout=timeout)
        except httpx.HTTPError:
            # ConnectError and TimeoutException are both httpx.HTTPError subclasses,
            # so this single clause covers all transport-level failures.
            return False
        return 200 <= resp.status_code < 300

    # === Team endpoints ===

    async def create_team(self, name: str, agent_ids: list[str]) -> str:
        """Create a team, then set members. Returns the Agamemnon team id."""
        response = await self._request_with_retry("POST", "/v1/teams", json={"name": name})
        self._raise_for_status(response)
        team_id = str(_require(response.json(), "team", "id", context="create_team"))
        if agent_ids:
            r2 = await self._request_with_retry(
                "PUT", f"/v1/teams/{team_id}", json={"agentIds": agent_ids}
            )
            self._raise_for_status(r2)
        return team_id

    async def delete_team(self, team_id: str) -> None:
        """Delete a team."""
        response = await self._request_with_retry("DELETE", f"/v1/teams/{team_id}")
        self._raise_for_status(response)

    # === Task endpoints ===

    async def create_task(
        self,
        team_id: str,
        spec: TaskSpec,
        blocked_by_ids: list[str] | None = None,
        assignee_agent_id: str | None = None,
    ) -> str:
        """Create a task within a team. Returns the Agamemnon task id.

        Args:
            team_id: The Agamemnon team ID.
            spec: The task specification.
            blocked_by_ids: Optional list of Agamemnon task IDs this task is blocked by.
            assignee_agent_id: Resolved Agamemnon agent ID to assign the task to.
                If provided, takes precedence over spec.assign_to (which is a name).
        """
        payload: dict[str, object] = {
            "subject": spec.subject,
            "description": spec.description,
        }
        if assignee_agent_id is not None:
            payload["assigneeAgentId"] = assignee_agent_id
        elif spec.assign_to:
            # spec.assign_to is a workflow-author-facing agent NAME, not an
            # Agamemnon agent ID. Silently posting it as assigneeAgentId would
            # produce a 4xx (or worse, an orphaned task) at runtime — fail fast
            # so the caller is forced to resolve the name first (see #198).
            raise ValueError(
                f"Task {spec.subject!r} has assign_to={spec.assign_to!r} but no "
                "assignee_agent_id was supplied. Callers must resolve the agent "
                "name to an Agamemnon agent ID before invoking create_task()."
            )
        if blocked_by_ids:
            payload["blockedBy"] = blocked_by_ids

        response = await self._request_with_retry(
            "POST", f"/v1/teams/{team_id}/tasks", json=payload
        )
        self._raise_for_status(response)
        return str(_require(response.json(), "task", "id", context="create_task"))

    async def update_task(
        self,
        team_id: str,
        task_id: str,
        status: str | None = None,
        assignee_agent_id: str | None = None,
    ) -> dict[str, object]:
        """Update a task's status or assignment."""
        payload: dict[str, object] = {}
        if status is not None:
            payload["status"] = status
        if assignee_agent_id is not None:
            payload["assigneeAgentId"] = assignee_agent_id

        response = await self._request_with_retry(
            "PUT", f"/v1/teams/{team_id}/tasks/{task_id}", json=payload
        )
        self._raise_for_status(response)
        body: dict[str, object] = response.json()
        return body

    async def get_tasks(self, team_id: str) -> list[dict[str, object]]:
        """List all tasks for a team."""
        response = await self._request_with_retry("GET", f"/v1/teams/{team_id}/tasks")
        self._raise_for_status(response)
        tasks: list[dict[str, object]] = _require(response.json(), "tasks", context="get_tasks")
        return tasks
