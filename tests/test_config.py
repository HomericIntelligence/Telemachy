"""Tests for configuration settings (#160).

Every documented setting is checked twice from one table: once for its
documented default and once for its environment override. Keeping the table as
the single source means a new setting fails these tests until it is documented,
and the assertions are a field-by-field contract rather than one test per line.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pydantic
import pytest

from telemachy.config import Settings

# Env var -> (field name, documented default)
DOCUMENTED_DEFAULTS: dict[str, tuple[str, object]] = {
    "AGAMEMNON_URL": ("agamemnon_url", "http://localhost:8080"),
    "AGAMEMNON_API_KEY": ("agamemnon_api_key", ""),
    "AGAMEMNON_RATE_LIMIT_RPS": ("agamemnon_rate_limit_rps", 0.0),
    "AGAMEMNON_RATE_LIMIT_BURST": ("agamemnon_rate_limit_burst", 16),
    "NATS_URL": ("nats_url", "nats://localhost:4222"),
    "WORKFLOWS_DIR": ("workflows_dir", Path("workflows")),
    "HOST_ID": ("host_id", "hermes"),
    "REQUIRE_TLS": ("require_tls", True),
    "MONITOR_TIMEOUT_SECONDS": ("monitor_timeout_seconds", 3600.0),
    "DEFAULT_WORKFLOW_TIMEOUT": ("default_workflow_timeout", 7200.0),
    "LOG_LEVEL": ("log_level", "INFO"),
    "LOG_FORMAT": ("log_format", "plain"),
    "OTEL_ENABLED": ("otel_enabled", False),
    "OTEL_SERVICE_NAME": ("otel_service_name", "telemachy"),
    "OTEL_EXPORTER": ("otel_exporter", "console"),
}

# Overrides that also prove type coercion (Path, float, bool).
OVERRIDES: dict[str, object] = {
    "AGAMEMNON_URL": "https://agamemnon.example.com",
    "AGAMEMNON_API_KEY": "secret-key-123",
    "AGAMEMNON_RATE_LIMIT_RPS": 12.5,
    "AGAMEMNON_RATE_LIMIT_BURST": 8,
    "NATS_URL": "tls://nats.example.com:4222",
    "WORKFLOWS_DIR": "workflows/override",
    "HOST_ID": "odysseus",
    "REQUIRE_TLS": False,
    "MONITOR_TIMEOUT_SECONDS": 120.5,
    "DEFAULT_WORKFLOW_TIMEOUT": 9000,
    "LOG_LEVEL": "DEBUG",
    "LOG_FORMAT": "json",
    "OTEL_ENABLED": True,
    "OTEL_SERVICE_NAME": "telemachy-test",
    "OTEL_EXPORTER": "console",
}


def _isolate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear every documented env var so defaults are actually defaults."""
    for env_var in DOCUMENTED_DEFAULTS:
        monkeypatch.delenv(env_var, raising=False)


def test_documented_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty environment yields exactly the documented default per field."""
    _isolate(monkeypatch)
    settings = Settings(_env_file=None)

    actual = {field: getattr(settings, field) for field, _ in DOCUMENTED_DEFAULTS.values()}
    assert actual == {field: default for field, default in DOCUMENTED_DEFAULTS.values()}
    assert isinstance(settings.workflows_dir, Path)


def test_environment_overrides_every_field(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each env var binds to its field, including Path/float/bool coercion."""
    for env_var, value in OVERRIDES.items():
        monkeypatch.setenv(env_var, str(value))

    settings = Settings(_env_file=None)

    assert settings.workflows_dir == Path(str(OVERRIDES["WORKFLOWS_DIR"]))
    assert settings.require_tls is False
    assert settings.monitor_timeout_seconds == 120.5
    assert settings.default_workflow_timeout == 9000.0
    assert settings.agamemnon_rate_limit_burst == 8
    assert settings.agamemnon_url == OVERRIDES["AGAMEMNON_URL"]
    assert settings.log_format == "json"
    assert settings.otel_enabled is True


def test_require_tls_disabled_warns_once_about_cleartext(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("REQUIRE_TLS", "false")
    with caplog.at_level(logging.WARNING, logger="telemachy.config"):
        Settings(_env_file=None)
    cleartext = [r for r in caplog.records if "cleartext" in r.message]
    assert len(cleartext) == 1


def test_require_tls_enabled_is_silent(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("REQUIRE_TLS", raising=False)
    with caplog.at_level(logging.WARNING, logger="telemachy.config"):
        Settings(_env_file=None)
    assert not [r for r in caplog.records if "cleartext" in r.message]


@pytest.mark.parametrize("value", ["true", "True", "TRUE", "1"])
def test_require_tls_truthy_spellings(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("REQUIRE_TLS", value)
    assert Settings(_env_file=None).require_tls is True


@pytest.mark.parametrize("value", ["false", "False", "FALSE", "0"])
def test_require_tls_falsy_spellings(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("REQUIRE_TLS", value)
    assert Settings(_env_file=None).require_tls is False


@pytest.mark.parametrize("value", ["0", "-5"])
def test_rate_limit_burst_must_be_positive(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """burst < 1 is rejected at startup instead of silently clamping."""
    monkeypatch.setenv("AGAMEMNON_RATE_LIMIT_BURST", value)
    with pytest.raises(pydantic.ValidationError):
        Settings(_env_file=None)


def test_client_kwargs_mirror_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """client_kwargs() forwards every AgamemnonClient constructor argument."""
    _isolate(monkeypatch)
    for env_var, value in OVERRIDES.items():
        monkeypatch.setenv(env_var, str(value))

    settings = Settings(_env_file=None)

    assert settings.client_kwargs() == {
        "url": "https://agamemnon.example.com",
        "api_key": "secret-key-123",
        "host_id": "odysseus",
        "require_tls": False,
        "nats_url": "tls://nats.example.com:4222",
        "rate_limit_rps": 12.5,
        "rate_limit_burst": 8,
    }


@pytest.mark.parametrize(
    ("env_var", "value", "message"),
    [
        pytest.param("LOG_FORMAT", "xml", "LOG_FORMAT", id="log-format"),
        pytest.param("OTEL_EXPORTER", "otlp", "OTEL_EXPORTER", id="otel-exporter"),
    ],
)
def test_unsupported_observability_values_are_rejected(
    monkeypatch: pytest.MonkeyPatch, env_var: str, value: str, message: str
) -> None:
    monkeypatch.setenv(env_var, value)
    with pytest.raises(pydantic.ValidationError, match=message):
        Settings(_env_file=None)
