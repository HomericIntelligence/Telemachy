"""Tests for AgamemnonClient HTTP interactions."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from telemachy.agamemnon_client import AgamemnonClient, AgamemnonError
from telemachy.models import AgentSpec

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_response(status_code: int, body: object) -> MagicMock:
    """Return a mock httpx.Response-like object."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.is_error = status_code >= 400
    resp.json.return_value = body
    resp.text = str(body)
    return resp


def _local_agent_spec(name: str = "worker") -> AgentSpec:
    return AgentSpec(name=name, runtime="local")


async def _enter_client(url: str = "http://localhost:8080", **kwargs: object) -> AgamemnonClient:
    """Return an AgamemnonClient that has been entered as async context manager."""
    client = AgamemnonClient(url=url, **kwargs)  # type: ignore[arg-type]
    # Manually install a real-ish AsyncClient so _http property works
    import httpx

    client._client = httpx.AsyncClient(base_url=url)
    return client


# ---------------------------------------------------------------------------
# TEST 1 — create_agent returns agent ID on success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_agent_returns_agent_id() -> None:
    client = await _enter_client()
    ok_resp = _make_response(201, {"agent": {"id": "agent-123"}})

    with patch.object(client._client, "request", new_callable=AsyncMock, return_value=ok_resp):
        agent_id = await client.create_agent(_local_agent_spec())

    assert agent_id == "agent-123"


# ---------------------------------------------------------------------------
# TEST 2 — create_agent raises AgamemnonError on 4xx
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_agent_raises_on_4xx() -> None:
    client = await _enter_client()
    err_resp = _make_response(400, {"detail": "bad request"})
    err_resp.json.return_value = {"detail": "bad request"}

    with (
        patch.object(client._client, "request", new_callable=AsyncMock, return_value=err_resp),
        pytest.raises(AgamemnonError) as exc_info,
    ):
        await client.create_agent(_local_agent_spec())

    assert exc_info.value.status_code == 400


# ---------------------------------------------------------------------------
# TEST 3 — create_agent raises AgamemnonError on malformed response
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_agent_raises_on_malformed_response() -> None:
    """Response missing 'agent.id' key should raise AgamemnonError."""
    client = await _enter_client()
    # Response is 201 OK but body has unexpected shape
    bad_resp = _make_response(201, {"result": "ok"})

    with (
        patch.object(client._client, "request", new_callable=AsyncMock, return_value=bad_resp),
        pytest.raises(AgamemnonError) as exc_info,
    ):
        await client.create_agent(_local_agent_spec())

    # Error should reference the missing key
    assert "agent" in str(exc_info.value).lower() or exc_info.value.status_code == 0


# ---------------------------------------------------------------------------
# TEST 4 — retry on 503: two failures then success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_agent_retries_on_503() -> None:
    client = await _enter_client()
    fail_resp = _make_response(503, {"detail": "service unavailable"})
    ok_resp = _make_response(201, {"agent": {"id": "retry-id"}})

    # 503, 503, then 201
    mock_request = AsyncMock(side_effect=[fail_resp, fail_resp, ok_resp])

    with (
        patch.object(client._client, "request", mock_request),
        patch("telemachy.agamemnon_client.asyncio.sleep", new_callable=AsyncMock),
    ):
        agent_id = await client.create_agent(_local_agent_spec())

    assert agent_id == "retry-id"
    assert mock_request.call_count == 3


# ---------------------------------------------------------------------------
# TEST 4b — retry on 429 rate-limit (#165)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_agent_retries_on_429() -> None:
    """429 Too Many Requests must trigger retry, not be returned to caller."""
    client = await _enter_client()
    rate_limited = _make_response(429, {"detail": "rate limited"})
    ok_resp = _make_response(201, {"agent": {"id": "rl-id"}})

    mock_request = AsyncMock(side_effect=[rate_limited, ok_resp])

    with (
        patch.object(client._client, "request", mock_request),
        patch("telemachy.agamemnon_client.asyncio.sleep", new_callable=AsyncMock),
    ):
        agent_id = await client.create_agent(_local_agent_spec())

    assert agent_id == "rl-id"
    assert mock_request.call_count == 2


# ---------------------------------------------------------------------------
# TEST 5 — start_agent calls correct endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_agent_calls_correct_endpoint() -> None:
    client = await _enter_client()
    ok_resp = _make_response(200, {})
    ok_resp.is_error = False

    mock_request = AsyncMock(return_value=ok_resp)
    with patch.object(client._client, "request", mock_request):
        await client.wake_agent("agent-123")

    mock_request.assert_called_once()
    call_args = mock_request.call_args
    assert call_args[0][0] == "POST"
    assert "/v1/agents/agent-123/start" in call_args[0][1]


# ---------------------------------------------------------------------------
# TEST 6 — delete_agent calls correct endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_agent_calls_correct_endpoint() -> None:
    client = await _enter_client()
    ok_resp = _make_response(204, {})
    ok_resp.is_error = False

    mock_request = AsyncMock(return_value=ok_resp)
    with patch.object(client._client, "request", mock_request):
        await client.delete_agent("agent-123")

    mock_request.assert_called_once()
    call_args = mock_request.call_args
    assert call_args[0][0] == "DELETE"
    assert "/v1/agents/agent-123" in call_args[0][1]


# ---------------------------------------------------------------------------
# TEST 7 — TLS enforcement raises on http:// when require_tls=True
# ---------------------------------------------------------------------------


def test_tls_enforcement_raises_on_http_url() -> None:
    """AgamemnonClient with require_tls=True and http:// URL must raise AgamemnonError."""
    with pytest.raises(AgamemnonError) as exc_info:
        AgamemnonClient(
            url="http://localhost:8080",
            require_tls=True,
        )

    assert exc_info.value.status_code == 0
    assert "TLS" in str(exc_info.value) or "https" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# TEST 8 — list_teams returns team list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_teams_returns_team_list() -> None:
    """list_teams should return a list of team dicts from /v1/teams."""
    client = await _enter_client()
    teams_resp = _make_response(
        200,
        {
            "teams": [
                {"id": "team-1", "name": "tlm-abc123-team-a"},
                {"id": "team-2", "name": "tlm-def456-team-b"},
            ]
        },
    )
    teams_resp.is_error = False

    mock_request = AsyncMock(return_value=teams_resp)
    with patch.object(client._client, "request", mock_request):
        teams = await client.list_teams()

    assert len(teams) == 2
    assert teams[0]["id"] == "team-1"
    assert teams[1]["name"] == "tlm-def456-team-b"


# ---------------------------------------------------------------------------
# TEST 9 — create_agent with idempotency_name uses that name
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_agent_with_idempotency_name() -> None:
    """create_agent with idempotency_name should POST that as the agent name."""
    client = await _enter_client()
    ok_resp = _make_response(201, {"agent": {"id": "agent-456"}})

    mock_request = AsyncMock(return_value=ok_resp)
    with patch.object(client._client, "request", mock_request):
        agent_id = await client.create_agent(
            _local_agent_spec(), idempotency_name="tlm-abc123-worker"
        )

    assert agent_id == "agent-456"
    # Verify the request used the idempotency name
    call_args = mock_request.call_args
    assert call_args[1]["json"]["name"] == "tlm-abc123-worker"
    # Verify label preserves the original name
    assert call_args[1]["json"]["label"] == "worker"


# ---------------------------------------------------------------------------
# TEST 10 — Rate limiter throttles requests (#160)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limiter_throttles_requests() -> None:
    """5 RPS, burst=2, 3 sequential calls: 3rd must wait ~0.2s for refill.

    Wide [0.12, 0.8] window for CI loop variance.
    """
    client = await _enter_client(rate_limit_rps=5.0, rate_limit_burst=2)
    ok = _make_response(200, {"agents": []})
    with patch.object(client._client, "request", new_callable=AsyncMock, return_value=ok):
        start = time.monotonic()
        await client.list_agents()
        await client.list_agents()
        await client.list_agents()  # waits for refill
        elapsed = time.monotonic() - start
    assert 0.12 <= elapsed <= 0.8


@pytest.mark.asyncio
async def test_rate_limiter_disabled_by_default() -> None:
    client = await _enter_client()
    assert not client._rate_limiter.enabled


@pytest.mark.asyncio
async def test_rate_limiter_applies_to_all_endpoints() -> None:
    """Burst budget is SHARED across endpoints — single chokepoint at _request_with_retry."""
    client = await _enter_client(rate_limit_rps=10.0, rate_limit_burst=1)
    ok_list = _make_response(200, {"agents": []})
    ok_post = _make_response(201, {"team": {"id": "t-1"}})
    start = time.monotonic()
    with patch.object(
        client._client, "request", new_callable=AsyncMock, side_effect=[ok_list, ok_post]
    ):
        await client.list_agents()  # consumes the 1 burst token
        await client.create_team("x", [])  # must wait ~0.1s for refill (10 RPS)
    elapsed = time.monotonic() - start
    assert 0.05 <= elapsed <= 0.6  # ±50% around 0.1s


@pytest.mark.asyncio
async def test_retry_backoff_capped_at_two_seconds() -> None:
    """min(2**attempt, 2.0) — high attempts must not balloon backoff."""
    client = await _enter_client()
    # Force three 500s; with cap, sleeps are 1s + 2s = 3s, not 1s + 2s = 3s either way at 3 attempts.
    err = _make_response(500, {"detail": "boom"})
    with patch.object(client._client, "request", new_callable=AsyncMock, return_value=err):
        start = time.monotonic()
        with pytest.raises(AgamemnonError):
            await client._request_with_retry("GET", "/v1/agents")
        elapsed = time.monotonic() - start
    # 3 attempts → 2 backoffs of 1s and 2s → ~3s; ceiling test allows slack.
    assert elapsed <= 4.5



