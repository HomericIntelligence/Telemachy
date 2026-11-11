"""Tests for the NATS task-lifecycle monitor.

The monitor has one job: turn broker facts into a monotonic, terminal-sticky
per-task status plus an awaitable event. These tests target that contract —
status transitions, envelope handling, and connection lifecycle — rather than
each accessor in isolation.
"""

from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from telemachy.nats_monitor import NatsMonitor, NatsUnavailableError


def _monitor() -> NatsMonitor:
    return NatsMonitor("nats://localhost:4222")


def _msg(subject: str, payload: object) -> MagicMock:
    msg = MagicMock()
    msg.subject = subject
    msg.data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return msg


def _envelope(data: object) -> dict[str, object]:
    return {
        "schema_version": 1,
        "event": "task.lifecycle",
        "data": data,
        "timestamp": "2026-06-03T00:00:00Z",
        "request_id": "req-123",
    }


# === subscription and state accessors ===


async def test_subscribe_team_uses_task_family_wildcard() -> None:
    monitor = _monitor()
    nc = AsyncMock()
    monitor._nc = nc

    await monitor.subscribe_team("team-001")

    nc.subscribe.assert_awaited_once()
    assert nc.subscribe.call_args.args[0] == "hi.tasks.team-001.*.*"


def test_state_accessors_start_empty_and_are_stable_per_subject() -> None:
    monitor = _monitor()

    first = monitor.terminal_event("Task 1")

    assert isinstance(first, asyncio.Event)
    assert not first.is_set()
    assert monitor.terminal_event("Task 1") is first
    assert monitor.terminal_event("Task 2") is not first
    assert monitor.latest_status("Task 1") is None


def test_record_status_is_terminal_sticky() -> None:
    """A terminal status can move to another terminal status but never downgrade."""
    monitor = _monitor()

    monitor.record_status("Task 1", "in_progress")
    assert monitor.latest_status("Task 1") == "in_progress"
    assert not monitor.terminal_event("Task 1").is_set()

    monitor.record_status("Task 1", "completed")
    assert monitor.terminal_event("Task 1").is_set()

    monitor.record_status("Task 1", "in_progress")
    assert monitor.latest_status("Task 1") == "completed"

    monitor.record_status("Task 1", "failed")
    assert monitor.latest_status("Task 1") == "failed"

    monitor.record_status("", "completed")
    assert monitor.latest_status("") is None


async def test_notify_submitted_wakes_one_waiter() -> None:
    monitor = _monitor()
    woke = asyncio.Event()

    async def waiter() -> None:
        await monitor.submitted_event.wait()
        woke.set()

    task = asyncio.create_task(waiter())
    await asyncio.sleep(0.01)

    monitor.notify_submitted()

    await asyncio.wait_for(woke.wait(), timeout=1.0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


# === envelope handling ===


@pytest.mark.parametrize(
    ("subject", "data", "expected"),
    [
        pytest.param(
            "hi.tasks.t1.task-1.completed",
            {"subject": "Task 1", "status": "completed"},
            "completed",
            id="explicit-status",
        ),
        pytest.param(
            "hi.tasks.t1.task-1.failed",
            {"subject": "Task 1"},
            "failed",
            id="verb-fallback",
        ),
    ],
)
async def test_handle_msg_records_task_status(
    subject: str, data: dict[str, object], expected: str
) -> None:
    monitor = _monitor()

    await monitor._handle_msg(_msg(subject, _envelope(data)))

    assert monitor.latest_status("Task 1") == expected
    if expected in {"completed", "failed"}:
        assert monitor.terminal_event("Task 1").is_set()


@pytest.mark.parametrize(
    ("payload", "warning"),
    [
        pytest.param(b"not valid json", "malformed NATS payload", id="bad-json"),
        pytest.param(_envelope(42), "unexpected NATS envelope", id="non-dict-data"),
        pytest.param(
            _envelope({"subject": "", "status": "completed"}), "no data.subject", id="empty-subject"
        ),
    ],
)
async def test_malformed_envelope_is_logged_and_dropped(
    payload: object, warning: str, caplog: pytest.LogCaptureFixture
) -> None:
    monitor = _monitor()

    with caplog.at_level(logging.WARNING):
        await monitor._handle_msg(_msg("hi.tasks.t1.task-1.completed", payload))

    assert warning in caplog.text
    assert monitor._latest_status == {}


# === connection lifecycle ===


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(ConnectionRefusedError("refused"), id="refused"),
        pytest.param(TimeoutError(), id="timeout"),
    ],
)
async def test_unusable_broker_raises_nats_unavailable(failure: Exception) -> None:
    monitor = _monitor()

    async def fail(*_args: object, **_kwargs: object) -> object:
        raise failure

    with (
        patch("nats.connect", side_effect=fail),
        pytest.raises(NatsUnavailableError, match="failed to connect"),
    ):
        async with monitor:
            pass


@pytest.mark.parametrize(
    ("nc_open", "broken", "expected"),
    [
        pytest.param(True, False, True, id="healthy"),
        pytest.param(False, False, False, id="closed"),
        pytest.param(True, True, False, id="broken-flag"),
        pytest.param(None, False, False, id="no-client"),
    ],
)
async def test_connected_reflects_client_and_break_flag(
    nc_open: bool | None, broken: bool, expected: bool
) -> None:
    monitor = _monitor()
    if nc_open is not None:
        nc = AsyncMock()
        nc.is_closed = not nc_open
        monitor._nc = nc
    if broken:
        monitor._broken.set()

    assert monitor.connected is expected


async def test_disconnect_callback_marks_monitor_broken() -> None:
    monitor = _monitor()
    nc = AsyncMock()
    nc.is_closed = False
    monitor._nc = nc

    await monitor._on_disconnected()

    assert monitor.connected is False


@pytest.mark.parametrize(
    ("drain_error", "logged"),
    [
        pytest.param(None, None, id="drains"),
        pytest.param(Exception("Drain failed"), "NATS drain failed", id="drain-error"),
    ],
)
async def test_exit_drains_and_never_raises(
    drain_error: Exception | None, logged: str | None, caplog: pytest.LogCaptureFixture
) -> None:
    monitor = _monitor()
    nc = AsyncMock()
    nc.is_closed = False
    nc.drain.side_effect = drain_error
    monitor._nc = nc

    with caplog.at_level(logging.WARNING):
        await monitor.__aexit__(None, None, None)

    nc.drain.assert_awaited_once()
    if logged is None:
        assert not caplog.records
    else:
        assert logged in caplog.text
    assert monitor._nc is None
