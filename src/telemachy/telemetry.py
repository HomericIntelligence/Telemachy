"""Observability primitives: correlation IDs, structured logging, metrics, and tracing."""

from __future__ import annotations

import json
import logging
import sys
import threading
from collections.abc import Sequence
from contextvars import ContextVar
from typing import Any

from opentelemetry import trace
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import Tracer
from prometheus_client import Counter, Histogram, start_http_server

# === Correlation context ===

workflow_id_var: ContextVar[str] = ContextVar("workflow_id", default="-")
workflow_name_var: ContextVar[str] = ContextVar("workflow_name", default="-")


class WorkflowContextLogFilter(logging.Filter):
    """Inject workflow_id and workflow_name from contextvars into every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.workflow_id = workflow_id_var.get("-")
        record.workflow_name = workflow_name_var.get("-")
        return True


class JsonFormatter(logging.Formatter):
    """Format log records as JSON, including workflow_id from context."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "workflow_id": getattr(record, "workflow_id", "-"),
            "workflow_name": getattr(record, "workflow_name", "-"),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class SafePlainFormatter(logging.Formatter):
    """%-format formatter that defensively defaults workflow_id/workflow_name before rendering."""

    def format(self, record: logging.LogRecord) -> str:
        if not hasattr(record, "workflow_id"):
            record.workflow_id = "-"
        if not hasattr(record, "workflow_name"):
            record.workflow_name = "-"
        return super().format(record)


class TelemachyDefaultHandler(logging.StreamHandler):
    """Marker subclass so _setup_logging can identify and replace its own handler."""


# === Metrics ===

_metrics_lock = threading.Lock()
_started_metrics_ports: set[int] = set()


def setup_metrics(port: int) -> None:
    """Start the Prometheus /metrics HTTP server. Idempotent in-process.

    Single-process assumption: Telemachy is a one-shot CLI. Multi-process
    forking is not supported by this module; a fresh process resets state.
    """
    with _metrics_lock:
        if port in _started_metrics_ports:
            return
        start_http_server(port)
        _started_metrics_ports.add(port)


WORKFLOWS_STARTED = Counter(
    "telemachy_workflows_started_total", "Workflows started", ["workflow_name"]
)
WORKFLOWS_COMPLETED = Counter(
    "telemachy_workflows_completed_total",
    "Workflows reaching a terminal status",
    ["workflow_name", "status"],
)
WORKFLOW_DURATION = Histogram(
    "telemachy_workflow_duration_seconds",
    "End-to-end workflow duration",
    ["workflow_name", "status"],
)
TASKS_TOTAL = Counter(
    "telemachy_tasks_total",
    "Tasks observed reaching a terminal state",
    ["status"],
)
AGAMEMNON_REQUESTS = Counter(
    "telemachy_agamemnon_requests_total",
    "Agamemnon HTTP requests",
    ["method", "endpoint", "status_code"],
)
AGAMEMNON_LATENCY = Histogram(
    "telemachy_agamemnon_request_seconds",
    "Agamemnon HTTP request latency",
    ["method", "endpoint"],
)


# === Tracing ===

_tracing_lock = threading.Lock()
_started_tracing_services: set[str] = set()


class LiveStreamConsoleSpanExporter(ConsoleSpanExporter):
    """Console exporter that resolves ``sys.stdout`` at export time.

    ``ConsoleSpanExporter`` binds ``sys.stdout`` when it is defined, so a
    caller that swaps stdout afterwards (pytest capture does) leaves the
    exporter writing to a stream that is later closed. Reporting a batch as
    failed when the resolved stream is gone keeps that from raising
    ``ValueError: I/O operation on closed file`` out of the export thread.
    """

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        stream = sys.stdout
        if stream is None or getattr(stream, "closed", False):
            return SpanExportResult.FAILURE
        self.out = stream
        return super().export(spans)


def setup_tracing(service_name: str) -> None:
    """Install a TracerProvider with the ConsoleSpanExporter.

    Caller is responsible for value-validation (handled in
    telemachy.config.Settings). OTLP exporter is a planned follow-up;
    not wired to avoid pulling grpcio.
    """
    with _tracing_lock:
        if service_name in _started_tracing_services:
            return
        provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
        provider.add_span_processor(BatchSpanProcessor(LiveStreamConsoleSpanExporter()))
        trace.set_tracer_provider(provider)
        HTTPXClientInstrumentor().instrument()
        _started_tracing_services.add(service_name)


def shutdown_tracing() -> None:
    """Flush and stop the TracerProvider installed by :func:`setup_tracing`.

    ``TracerProvider`` registers its own ``atexit`` flush, which runs after any
    caller that owns stdout (a pytest session, a closed pipe) has torn it down.
    Shutting down explicitly flushes while the stream is still usable; the
    provider stays shut down for the rest of the process.
    """
    with _tracing_lock:
        provider = trace.get_tracer_provider()
        if isinstance(provider, TracerProvider):
            provider.shutdown()


def get_tracer() -> Tracer:
    """Return the current Telemachy tracer (per-call, not cached)."""
    return trace.get_tracer("telemachy")
