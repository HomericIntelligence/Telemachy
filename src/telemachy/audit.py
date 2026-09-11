"""Append-only JSONL audit log for workflow execution events.

Sinks are CONSTRUCTED, not module-global. Inject into WorkflowExecutor.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import logging
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, TextIO

logger = logging.getLogger(__name__)

_GENESIS_HASH = "0" * 64


class AuditChainError(RuntimeError):
    """Raised when an existing audit log cannot be resumed (corrupt tail / missing hash)."""


class AuditSinkProtocol(Protocol):
    """Protocol for audit sinks — dependency-injected into WorkflowExecutor."""

    def emit(self, event_type: str, *, workflow_id: str | None = None, **fields: Any) -> None:
        """Emit one audit event."""

    def close(self) -> None:
        """Close the sink."""


class NullSink:
    """No-op sink. Used when AUDIT_LOG_PATH is unset or sink construction failed."""

    def emit(self, event_type: str, *, workflow_id: str | None = None, **fields: Any) -> None:
        return None

    def close(self) -> None:
        return None


class AuditSink:
    """Append-only JSONL sink with optional SHA-256 hash chain.

    Continuity: if `path` already exists and is non-empty, the previous run's
    last `hash` is read and used as the seed for `prev_hash`. This prevents a
    process restart from silently creating a false chain start at the zero
    genesis value (see issue #187 prior review).
    """

    def __init__(
        self,
        *,
        path: Path,
        host_id: str,
        hash_chain: bool = True,
    ) -> None:
        self._path: Path = path
        self._chain: bool = hash_chain
        self._lock = threading.Lock()
        self._fh: TextIO | None = None
        # USER works on POSIX; USERNAME is the Windows fallback. CI sandboxes that
        # strip both env vars get "unknown" — fidelity loss is logged once below.
        user = os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"
        if user == "unknown":
            logger.warning("Audit actor.user resolved to 'unknown' — USER/USERNAME unset")
        self._actor: dict[str, str] = {"host_id": host_id, "user": user}
        self._prev_hash: str = _GENESIS_HASH
        if self._chain:
            self._prev_hash = self._resume_chain()
        atexit.register(self.close)

    def _resume_chain(self) -> str:
        """Read the last JSON record's hash to resume the chain, or return genesis hash."""
        if not self._path.exists() or self._path.stat().st_size == 0:
            return _GENESIS_HASH
        last_line = ""
        with self._path.open("rb") as fh:
            for line in fh:
                if line.strip():
                    last_line = line.decode("utf-8")
        if not last_line:
            return _GENESIS_HASH
        try:
            rec = json.loads(last_line)
            prev = rec["hash"]
        except (json.JSONDecodeError, KeyError) as exc:
            raise AuditChainError(
                f"Cannot resume hash chain from {self._path}: last line missing 'hash'"
            ) from exc
        if not isinstance(prev, str) or len(prev) != 64:
            raise AuditChainError(f"Invalid prior hash in {self._path}: {prev!r}")
        return prev

    def _open(self) -> TextIO:
        """Lazily open the file in append mode."""
        if self._fh is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = self._path.open("a", encoding="utf-8")
        return self._fh

    def emit(self, event_type: str, *, workflow_id: str | None = None, **fields: Any) -> None:
        """Append a structured audit record (synchronously, no await possible)."""
        record: dict[str, Any] = {
            "timestamp": datetime.now(tz=UTC).isoformat(timespec="microseconds"),
            "event_type": event_type,
            "workflow_id": workflow_id,
            "actor": self._actor,
            "payload": fields,
        }
        if self._chain:
            record["prev_hash"] = self._prev_hash
            digest = hashlib.sha256(
                json.dumps(record, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()
            record["hash"] = digest
        line = json.dumps(record, sort_keys=True, default=str) + "\n"
        with self._lock:
            try:
                fh = self._open()
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
            except OSError as exc:
                logger.warning("Audit write failed for %s: %s", self._path, exc)
                return
            if self._chain:
                self._prev_hash = record["hash"]

    def close(self) -> None:
        """Close the file handle safely."""
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.close()
                finally:
                    self._fh = None


def build_sink_from_settings() -> AuditSinkProtocol:
    """Construct a sink from current settings. Returns NullSink on failure or when disabled."""
    from telemachy.config import settings

    if not settings.audit_log_path:
        return NullSink()
    try:
        return AuditSink(
            path=Path(settings.audit_log_path),
            host_id=settings.host_id,
            hash_chain=settings.audit_hash_chain,
        )
    except (AuditChainError, OSError) as exc:
        logger.warning("Audit sink disabled — construction failed: %s", exc)
        return NullSink()
