"""Connectivity and liveness behaviour (issues #161, #3).

``client.ping()`` remains a single-shot probe. Mid-workflow connectivity loss is
detected by the NATS event monitor, and the regression that matters is #161: a
lost event bus must still tear down agents and teams under the workflow's
declared teardown policy.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from telemachy.agamemnon_client import AgamemnonClient
from telemachy.executor import WorkflowExecutor
from telemachy.nats_monitor import NatsUnavailableError
from tests.conftest import make_executor_spec, make_mock_client, make_mock_monitor


def _disconnected_monitor() -> MagicMock:
    """A monitor that reports a mid-workflow disconnect: nothing goes terminal."""
    monitor = make_mock_monitor()
    monitor.connected = False
    monitor.latest_status = MagicMock(return_value="in_progress")
    monitor.terminal_event = MagicMock(side_effect=lambda _subject: asyncio.Event())
    return monitor


# === ping() ===


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        pytest.param(MagicMock(status_code=200), True, id="2xx"),
        pytest.param(MagicMock(status_code=503), False, id="5xx"),
        pytest.param(httpx.TimeoutException("timeout"), False, id="timeout"),
        pytest.param(httpx.ConnectError("refused"), False, id="connect-error"),
    ],
)
@pytest.mark.asyncio
async def test_ping_reports_liveness(reply: object, expected: bool) -> None:
    """A probe returns True only for a 2xx answer; transport errors are False."""
    client = AgamemnonClient(url="http://localhost:8080")
    http = AsyncMock()
    if isinstance(reply, BaseException):
        http.request = AsyncMock(side_effect=reply)
    else:
        http.request = AsyncMock(return_value=reply)
    client._client = http

    assert await client.ping(timeout=5.0) is expected


# === event-bus connectivity loss ===


@pytest.mark.asyncio
async def test_mid_workflow_disconnect_fails_workflow() -> None:
    """A lost event bus fails the run and flags connectivity_failed."""
    client = make_mock_client()

    with patch("telemachy.executor.NatsMonitor", return_value=_disconnected_monitor()):
        state = await WorkflowExecutor(client, poll_interval=0.01).execute(make_executor_spec())

    assert state.status == "failed"
    assert state.connectivity_failed is True
    assert state.error is not None and "NATS connection lost" in state.error


@pytest.mark.asyncio
async def test_unreachable_broker_at_connect_fails_fast() -> None:
    """An unreachable broker fails before any monitoring work begins."""
    client = make_mock_client()
    monitor = make_mock_monitor()
    monitor.__aenter__.side_effect = NatsUnavailableError("NATS unreachable")

    with patch("telemachy.executor.NatsMonitor", return_value=monitor):
        state = await WorkflowExecutor(client, poll_interval=0.01).execute(make_executor_spec())

    assert state.status == "failed"
    assert state.connectivity_failed is True
    assert "NATS" in (state.error or "")


# === teardown policy under connectivity failure (#161) ===


@pytest.mark.parametrize(
    ("teardown", "expects_teardown"),
    [
        pytest.param("on_failure", True, id="on-failure"),
        pytest.param("on_completion", True, id="on-completion-resource-leak"),
        pytest.param("never", False, id="never"),
    ],
)
@pytest.mark.asyncio
async def test_connectivity_failure_applies_declared_teardown(
    teardown: str, expects_teardown: bool
) -> None:
    """Connectivity failure is a failure outcome: `on_completion` must still tear down.

    This is the #161 regression. Without it, agents and teams leak whenever the
    event bus drops under the default `on_completion` policy.
    """
    client = make_mock_client()

    with patch("telemachy.executor.NatsMonitor", return_value=_disconnected_monitor()):
        state = await WorkflowExecutor(client, poll_interval=0.01).execute(
            make_executor_spec(teardown=teardown)
        )

    assert state.status == "failed"
    assert state.connectivity_failed is True
    if expects_teardown:
        client.delete_agent.assert_called()
        client.delete_team.assert_called()
    else:
        client.delete_agent.assert_not_called()
        client.delete_team.assert_not_called()
