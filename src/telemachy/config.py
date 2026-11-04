"""Configuration settings loaded from environment variables or .env file."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TypedDict

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class AgamemnonClientKwargs(TypedDict):
    """Typed kwargs matching AgamemnonClient.__init__ parameters."""

    url: str
    api_key: str
    host_id: str
    require_tls: bool
    nats_url: str
    rate_limit_rps: float
    rate_limit_burst: int


class Settings(BaseSettings):
    """Application settings for Telemachy."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    agamemnon_url: str = "http://localhost:8080"
    agamemnon_api_key: str = ""
    agamemnon_rate_limit_rps: float = 0.0
    agamemnon_rate_limit_burst: int = Field(default=16, ge=1)
    nats_url: str = "nats://localhost:4222"
    workflows_dir: Path = Path("workflows")
    host_id: str = "hermes"
    require_tls: bool = True
    monitor_timeout_seconds: float = 3600.0
    monitor_max_polls: int = 7200
    healthcheck_interval_seconds: float = 15.0
    healthcheck_failure_threshold: int = 2
    healthcheck_timeout_seconds: float = 5.0
    log_level: str = "INFO"
    default_workflow_timeout: float = 7200.0
    log_format: str = "plain"
    otel_enabled: bool = False
    otel_service_name: str = "telemachy"
    otel_exporter: str = "console"

    @model_validator(mode="after")
    def validate_observability_settings(self) -> Settings:
        """Validate observability configuration."""
        if self.log_format not in {"plain", "json"}:
            raise ValueError(f"LOG_FORMAT must be 'plain' or 'json', got {self.log_format!r}")
        if self.otel_exporter != "console":
            raise ValueError(
                f"OTEL_EXPORTER={self.otel_exporter!r} not supported in this release. "
                "Only 'console' is wired; OTLP is a planned follow-up."
            )
        return self

    @model_validator(mode="after")
    def _warn_if_tls_disabled(self) -> Settings:
        if not self.require_tls:
            logger.warning(
                "REQUIRE_TLS=false set explicitly — Agamemnon traffic "
                "(including API key in Authorization header) will be "
                "transmitted in cleartext. Use only for local development."
            )
        return self

    def client_kwargs(self) -> AgamemnonClientKwargs:
        """Return keyword arguments for constructing an AgamemnonClient.

        Both ``cli.run`` and ``executor.run_workflow`` must use this so that
        client construction stays DRY and all settings are applied uniformly.
        """
        return {
            "url": self.agamemnon_url,
            "api_key": self.agamemnon_api_key,
            "host_id": self.host_id,
            "require_tls": self.require_tls,
            "nats_url": self.nats_url,
            "rate_limit_rps": self.agamemnon_rate_limit_rps,
            "rate_limit_burst": self.agamemnon_rate_limit_burst,
        }


settings = Settings()
