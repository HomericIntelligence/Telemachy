"""Tests for NATS event monitoring."""

from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from telemachy.nats_monitor import NatsMonitor, NatsUnavailableError


class TestNatsMonitorBasics:
    async def test_subscribe_team_uses_wildcard_subject(self) -> None:
        """Test that subscribe_team constructs the correct wildcard subject."""
        monitor = NatsMonitor("nats://localhost:4222")

        # Mock the NATS client
        mock_nc = AsyncMock()
        mock_sub = AsyncMock()
        mock_nc.subscribe.return_value = mock_sub
        monitor._nc = mock_nc

        await monitor.subscribe_team("team-001")

        # Verify subscription used the correct wildcard
        mock_nc.subscribe.assert_called_once()
        subject = mock_nc.subscribe.call_args[0][0]
        assert subject == "hi.tasks.team-001.*.*"

    async def test_terminal_event_returns_asyncio_event(self) -> None:
        """Test that terminal_event returns an asyncio.Event."""
        monitor = NatsMonitor("nats://localhost:4222")
        ev = monitor.terminal_event("Task 1")
        assert isinstance(ev, asyncio.Event)
        assert not ev.is_set()

    async def test_terminal_event_returns_same_event_on_multiple_calls(self) -> None:
        """Test that calling terminal_event twice returns the same Event."""
        monitor = NatsMonitor("nats://localhost:4222")
        ev1 = monitor.terminal_event("Task 1")
        ev2 = monitor.terminal_event("Task 1")
        assert ev1 is ev2

    async def test_latest_status_returns_none_initially(self) -> None:
        """Test that latest_status returns None for unknown subjects."""
        monitor = NatsMonitor("nats://localhost:4222")
        status = monitor.latest_status("Task 1")
        assert status is None

    def test_record_status_empty_subject_ignored(self) -> None:
        """Test that record_status ignores empty subjects."""
        monitor = NatsMonitor("nats://localhost:4222")
        monitor.record_status("", "completed")
        assert monitor.latest_status("") is None
        assert len(monitor._latest_status) == 0

    def test_record_status_sets_status(self) -> None:
        """Test that record_status sets the status."""
        monitor = NatsMonitor("nats://localhost:4222")
        monitor.record_status("Task 1", "in_progress")
        assert monitor.latest_status("Task 1") == "in_progress"

    def test_terminal_status_is_sticky(self) -> None:
        """Test that terminal status cannot be overwritten by non-terminal status."""
        monitor = NatsMonitor("nats://localhost:4222")
        monitor.record_status("Task 1", "completed")
        assert monitor.latest_status("Task 1") == "completed"
        assert monitor.terminal_event("Task 1").is_set()

        # Try to downgrade to non-terminal status
        monitor.record_status("Task 1", "in_progress")
        # Should remain completed (sticky)
        assert monitor.latest_status("Task 1") == "completed"
        assert monitor.terminal_event("Task 1").is_set()

    def test_record_status_terminal_to_terminal_transition_allowed(self) -> None:
        """Test that one terminal status can transition to another terminal status."""
        monitor = NatsMonitor("nats://localhost:4222")
        monitor.record_status("Task 1", "completed")
        assert monitor.latest_status("Task 1") == "completed"

        # Transition to another terminal status
        monitor.record_status("Task 1", "failed")
        assert monitor.latest_status("Task 1") == "failed"

    async def test_notify_submitted_sends_pulse(self) -> None:
        """Test that notify_submitted wakes a waiting coroutine."""
        monitor = NatsMonitor("nats://localhost:4222")

        # Schedule a task that waits on the event
        wait_done = asyncio.Event()

        async def waiter() -> None:
            await monitor.submitted_event.wait()
            wait_done.set()

        task = asyncio.create_task(waiter())
        await asyncio.sleep(0.01)  # Let the waiter start

        # Notify and check that the waiter woke up
        monitor.notify_submitted()
        await asyncio.wait_for(wait_done.wait(), timeout=1.0)

        task.cancel()
        # Wait for the cancelled task to finish unwinding; CancelledError
        # is expected and swallowed by gather.
        await asyncio.gather(task, return_exceptions=True)


class TestNatsMonitorMessageHandling:
    async def test_handle_msg_sets_event_on_completed(self) -> None:
        """Test that handling a completed message sets the terminal_event."""
        monitor = NatsMonitor("nats://localhost:4222")

        # Create a mock message
        msg = MagicMock()
        msg.subject = "hi.tasks.team-001.task-123.completed"
        payload = {
            "schema_version": 1,
            "event": "task.completed",
            "data": {
                "subject": "Task 1",
                "status": "completed",
                "task_id": "t1",
                "team_id": "tm1",
            },
            "timestamp": "2026-06-03T00:00:00Z",
            "request_id": "req-123",
        }
        msg.data = json.dumps(payload).encode()

        await monitor._handle_msg(msg)

        assert monitor.latest_status("Task 1") == "completed"
        assert monitor.terminal_event("Task 1").is_set()

    async def test_handle_msg_falls_back_to_verb_when_status_missing(self) -> None:
        """Test that status falls back to the subject verb when data.status is absent."""
        monitor = NatsMonitor("nats://localhost:4222")

        msg = MagicMock()
        msg.subject = "hi.tasks.team-001.task-123.failed"
        payload = {
            "schema_version": 1,
            "event": "task.failed",
            "data": {
                "subject": "Task 1",
                # status is intentionally absent
                "task_id": "t1",
            },
            "timestamp": "2026-06-03T00:00:00Z",
            "request_id": "req-123",
        }
        msg.data = json.dumps(payload).encode()

        await monitor._handle_msg(msg)

        assert monitor.latest_status("Task 1") == "failed"

    async def test_handle_msg_warns_and_drops_when_subject_empty(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test that a message with empty data.subject logs a warning and is dropped."""
        monitor = NatsMonitor("nats://localhost:4222")

        msg = MagicMock()
        msg.subject = "hi.tasks.team-001.task-123.completed"
        payload = {
            "schema_version": 1,
            "event": "task.completed",
            "data": {
                "subject": "",  # Empty
                "status": "completed",
            },
            "timestamp": "2026-06-03T00:00:00Z",
            "request_id": "req-123",
        }
        msg.data = json.dumps(payload).encode()

        with caplog.at_level(logging.WARNING):
            await monitor._handle_msg(msg)

        assert len(monitor._events) == 0
        assert "no data.subject" in caplog.text

    async def test_handle_msg_warns_on_non_dict_data(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test that a malformed data (non-dict) logs a warning."""
        monitor = NatsMonitor("nats://localhost:4222")

        msg = MagicMock()
        msg.subject = "hi.tasks.team-001.task-123.completed"
        payload = {
            "schema_version": 1,
            "event": "task.completed",
            "data": 42,  # Not a dict
            "timestamp": "2026-06-03T00:00:00Z",
            "request_id": "req-123",
        }
        msg.data = json.dumps(payload).encode()

        with caplog.at_level(logging.WARNING):
            await monitor._handle_msg(msg)

        assert len(monitor._events) == 0
        assert "unexpected NATS envelope" in caplog.text

    async def test_handle_msg_warns_on_malformed_json(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test that malformed JSON is logged and dropped."""
        monitor = NatsMonitor("nats://localhost:4222")

        msg = MagicMock()
        msg.subject = "hi.tasks.team-001.task-123.completed"
        msg.data = b"not valid json"

        with caplog.at_level(logging.WARNING):
            await monitor._handle_msg(msg)

        assert len(monitor._events) == 0
        assert "malformed NATS payload" in caplog.text


class TestNatsMonitorConnection:
    async def test_connect_timeout_raises_nats_unavailable(self) -> None:
        """Test that a connection timeout raises NatsUnavailableError."""
        monitor = NatsMonitor("nats://localhost:4222")

        async def slow_connect(*args: object, **kwargs: object) -> object:
            await asyncio.sleep(10)
            raise Exception("Should not get here")

        with (
            patch("nats.connect", side_effect=slow_connect),
            pytest.raises(NatsUnavailableError, match="failed to connect"),
        ):
            async with monitor:
                pass

    async def test_connect_refusal_raises_nats_unavailable(self) -> None:
        """Test that a connection refusal raises NatsUnavailableError."""
        monitor = NatsMonitor("nats://localhost:4222")

        async def refuse_connect(*args: object, **kwargs: object) -> object:
            raise ConnectionRefusedError("Connection refused")

        with (
            patch("nats.connect", side_effect=refuse_connect),
            pytest.raises(NatsUnavailableError, match="failed to connect"),
        ):
            async with monitor:
                pass

    async def test_disconnect_callback_flips_connected_to_false(self) -> None:
        """Test that _on_disconnected sets _broken flag."""
        monitor = NatsMonitor("nats://localhost:4222")
        mock_nc = AsyncMock()
        mock_nc.is_closed = False
        monitor._nc = mock_nc
        monitor._broken = asyncio.Event()

        assert monitor.connected is True
        await monitor._on_disconnected()
        assert monitor.connected is False

    async def test_drain_called_on_aexit(self) -> None:
        """Test that __aexit__ calls drain on the NATS client."""
        monitor = NatsMonitor("nats://localhost:4222")
        mock_nc = AsyncMock()
        mock_nc.is_closed = False
        monitor._nc = mock_nc

        await monitor.__aexit__(None, None, None)

        mock_nc.drain.assert_called_once()

    async def test_drain_failure_logged_not_raised(self, caplog: pytest.LogCaptureFixture) -> None:
        """Test that drain errors are logged but do not raise."""
        monitor = NatsMonitor("nats://localhost:4222")
        mock_nc = AsyncMock()
        mock_nc.is_closed = False
        mock_nc.drain.side_effect = Exception("Drain failed")
        monitor._nc = mock_nc

        with caplog.at_level(logging.WARNING):
            # Should not raise
            await monitor.__aexit__(None, None, None)

        assert "NATS drain failed" in caplog.text
        assert monitor._nc is None

    async def test_connected_property_true_when_healthy(self) -> None:
        """Test that connected returns True when NC is open and not broken."""
        monitor = NatsMonitor("nats://localhost:4222")
        mock_nc = AsyncMock()
        mock_nc.is_closed = False
        monitor._nc = mock_nc
        monitor._broken = asyncio.Event()

        assert monitor.connected is True

    async def test_connected_property_false_when_nc_is_none(self) -> None:
        """Test that connected returns False when _nc is None."""
        monitor = NatsMonitor("nats://localhost:4222")
        monitor._nc = None

        assert monitor.connected is False

    async def test_connected_property_false_when_broken(self) -> None:
        """Test that connected returns False when _broken is set."""
        monitor = NatsMonitor("nats://localhost:4222")
        mock_nc = AsyncMock()
        mock_nc.is_closed = False
        monitor._nc = mock_nc
        monitor._broken.set()

        assert monitor.connected is False
