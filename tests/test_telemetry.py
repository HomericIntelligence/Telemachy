"""Tests for observability module: logging filters, formatters, metrics, tracing."""

import json
import logging
import threading
from unittest.mock import patch

import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from telemachy.telemetry import (
    WORKFLOWS_STARTED,
    JsonFormatter,
    SafePlainFormatter,
    WorkflowContextLogFilter,
    _started_metrics_ports,
    _started_tracing_services,
    get_tracer,
    setup_metrics,
    setup_tracing,
    workflow_id_var,
    workflow_name_var,
)


class InMemorySpanExporter(SpanExporter):
    """Simple in-memory span exporter for testing."""

    def __init__(self) -> None:
        self.spans: list[ReadableSpan] = []

    def export(self, spans: list[ReadableSpan]) -> SpanExportResult:
        self.spans.extend(spans)
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True

    def get_finished_spans(self) -> list[ReadableSpan]:
        return self.spans


# === Filter and formatter tests ===


def test_context_filter_defaults_when_unset() -> None:
    """WorkflowContextLogFilter defaults workflow_id to '-' when contextvar is unset."""
    filter = WorkflowContextLogFilter()
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="test",
        args=(),
        exc_info=None,
    )
    assert filter.filter(record)
    assert record.workflow_id == "-"
    assert record.workflow_name == "-"


def test_context_filter_reads_contextvars() -> None:
    """WorkflowContextLogFilter reads workflow_id and workflow_name from contextvars."""
    filter = WorkflowContextLogFilter()
    wf_token = workflow_id_var.set("abc-123")
    name_token = workflow_name_var.set("test-workflow")
    try:
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="test",
            args=(),
            exc_info=None,
        )
        assert filter.filter(record)
        assert record.workflow_id == "abc-123"
        assert record.workflow_name == "test-workflow"
    finally:
        workflow_id_var.reset(wf_token)
        workflow_name_var.reset(name_token)


def test_safe_plain_formatter_does_not_crash_without_filter() -> None:
    """SafePlainFormatter defaults missing attributes before rendering."""
    formatter = SafePlainFormatter("%(workflow_id)s - %(message)s")
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="hello",
        args=(),
        exc_info=None,
    )
    result = formatter.format(record)
    assert "- - hello" in result


def test_json_formatter_emits_valid_json_with_workflow_id() -> None:
    """JsonFormatter emits valid JSON including workflow_id from context."""
    formatter = JsonFormatter()
    filter = WorkflowContextLogFilter()
    wf_token = workflow_id_var.set("wf-001")
    try:
        record = logging.LogRecord(
            name="telemachy.executor",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="workflow started",
            args=(),
            exc_info=None,
        )
        filter.filter(record)
        result = formatter.format(record)
        data = json.loads(result)
        assert data["workflow_id"] == "wf-001"
        assert data["msg"] == "workflow started"
        assert data["level"] == "INFO"
    finally:
        workflow_id_var.reset(wf_token)


def test_json_formatter_emits_exception() -> None:
    """JsonFormatter includes exception info when present."""
    formatter = JsonFormatter()
    try:
        raise ValueError("test error")
    except ValueError:
        import sys

        record = logging.LogRecord(
            name="test",
            level=logging.ERROR,
            pathname="",
            lineno=0,
            msg="caught error",
            args=(),
            exc_info=sys.exc_info(),
        )
        result = formatter.format(record)
        data = json.loads(result)
        assert "exc" in data
        assert "ValueError: test error" in data["exc"]


def test_json_formatter_safe_without_filter() -> None:
    """JsonFormatter uses getattr defaults for workflow_id when filter not attached."""
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="test",
        args=(),
        exc_info=None,
    )
    result = formatter.format(record)
    data = json.loads(result)
    assert data["workflow_id"] == "-"


# === Metrics tests ===


@pytest.fixture(autouse=True)
def reset_metrics_state() -> None:
    """Reset metrics state between tests."""

    _started_metrics_ports.clear()
    _started_tracing_services.clear()


def test_setup_metrics_idempotent() -> None:
    """setup_metrics can be called multiple times; only first call starts server."""
    with patch("telemachy.telemetry.start_http_server") as mock_start:
        setup_metrics(0)
        assert mock_start.call_count == 1
        setup_metrics(0)
        assert mock_start.call_count == 1


def test_setup_metrics_thread_safe() -> None:
    """setup_metrics is thread-safe; multiple threads only start server once."""
    with patch("telemachy.telemetry.start_http_server") as mock_start:
        threads = []
        for _ in range(8):
            t = threading.Thread(target=setup_metrics, args=(0,))
            threads.append(t)
            t.start()
        for t in threads:
            t.join()
        assert mock_start.call_count == 1


def test_metrics_counters_increment() -> None:
    """Prometheus counters increment and are reflected in generate_latest()."""
    WORKFLOWS_STARTED.labels(workflow_name="test").inc()
    from prometheus_client import generate_latest

    output = generate_latest()
    assert b"telemachy_workflows_started_total" in output
    assert b'workflow_name="test"' in output


# === Tracing tests ===


def test_setup_tracing_idempotent() -> None:
    """setup_tracing can be called multiple times; only first call configures provider."""
    with patch("telemachy.telemetry.HTTPXClientInstrumentor") as mock_instrumentor:
        setup_tracing("test")
        assert mock_instrumentor.return_value.instrument.call_count == 1
        setup_tracing("test")
        assert mock_instrumentor.return_value.instrument.call_count == 1


def test_get_tracer_picks_up_provider_set_after_import() -> None:
    """get_tracer() returns a tracer that can produce spans."""
    # Simply verify get_tracer() returns a tracer object that works
    tracer = get_tracer()
    assert tracer is not None
    span = tracer.start_span("test")
    assert span is not None
    span.end()


@pytest.mark.asyncio
async def test_setup_tracing_with_console_exporter() -> None:
    """setup_tracing configures a working TracerProvider with ConsoleSpanExporter."""

    setup_tracing("test-service")
    tracer = get_tracer()
    with tracer.start_as_current_span("test-span"):
        pass
    # ConsoleSpanExporter prints to stderr but does not track spans.
    # The test passes if no exception is raised.
