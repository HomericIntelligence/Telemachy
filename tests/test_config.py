"""Tests for configuration settings (#160)."""

from __future__ import annotations

import logging
from pathlib import Path

import pydantic
import pytest

from telemachy.config import Settings


def test_require_tls_false_emits_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Setting REQUIRE_TLS=false emits a cleartext WARNING."""
    monkeypatch.setenv("REQUIRE_TLS", "false")
    with caplog.at_level(logging.WARNING, logger="telemachy.config"):
        Settings(_env_file=None)
    assert any("cleartext" in record.message for record in caplog.records)


def test_require_tls_true_emits_no_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Default REQUIRE_TLS=true emits no warning."""
    monkeypatch.delenv("REQUIRE_TLS", raising=False)
    with caplog.at_level(logging.WARNING, logger="telemachy.config"):
        Settings(_env_file=None)
    assert not any("cleartext" in record.message for record in caplog.records)


def test_require_tls_env_false() -> None:
    import os

    os.environ["REQUIRE_TLS"] = "0"
    try:
        assert Settings(_env_file=None).require_tls is False
    finally:
        del os.environ["REQUIRE_TLS"]


def test_require_tls_env_true() -> None:
    import os

    os.environ["REQUIRE_TLS"] = "1"
    try:
        assert Settings(_env_file=None).require_tls is True
    finally:
        del os.environ["REQUIRE_TLS"]


class TestDefaults:
    """Every field defaults to the value documented in AGENTS.md."""

    def test_agamemnon_url_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AGAMEMNON_URL", raising=False)
        assert Settings(_env_file=None).agamemnon_url == "http://localhost:8080"

    def test_agamemnon_api_key_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AGAMEMNON_API_KEY", raising=False)
        assert Settings(_env_file=None).agamemnon_api_key == ""

    def test_nats_url_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NATS_URL", raising=False)
        assert Settings(_env_file=None).nats_url == "nats://localhost:4222"

    def test_workflows_dir_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("WORKFLOWS_DIR", raising=False)
        s = Settings(_env_file=None)
        assert s.workflows_dir == Path("workflows")
        assert isinstance(s.workflows_dir, Path)

    def test_host_id_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HOST_ID", raising=False)
        assert Settings(_env_file=None).host_id == "hermes"

    def test_require_tls_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("REQUIRE_TLS", raising=False)
        assert Settings(_env_file=None).require_tls is True

    def test_log_level_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("LOG_LEVEL", raising=False)
        assert Settings(_env_file=None).log_level == "INFO"

    def test_monitor_timeout_seconds_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MONITOR_TIMEOUT_SECONDS", raising=False)
        assert Settings(_env_file=None).monitor_timeout_seconds == 3600.0

    def test_monitor_max_polls_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MONITOR_MAX_POLLS", raising=False)
        assert Settings(_env_file=None).monitor_max_polls == 7200

    def test_default_workflow_timeout_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DEFAULT_WORKFLOW_TIMEOUT", raising=False)
        assert Settings(_env_file=None).default_workflow_timeout == 7200.0


class TestEnvOverrides:
    """Each documented env var overrides its corresponding field."""

    def test_agamemnon_url_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGAMEMNON_URL", "https://agamemnon.example.com")
        assert Settings(_env_file=None).agamemnon_url == "https://agamemnon.example.com"

    def test_agamemnon_api_key_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGAMEMNON_API_KEY", "secret-key-123")
        assert Settings(_env_file=None).agamemnon_api_key == "secret-key-123"

    def test_nats_url_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NATS_URL", "tls://nats.example.com:4222")
        assert Settings(_env_file=None).nats_url == "tls://nats.example.com:4222"

    def test_workflows_dir_override_coerces_to_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("WORKFLOWS_DIR", str(tmp_path))
        s = Settings(_env_file=None)
        assert s.workflows_dir == tmp_path
        assert isinstance(s.workflows_dir, Path)

    def test_host_id_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOST_ID", "odysseus")
        assert Settings(_env_file=None).host_id == "odysseus"

    def test_log_level_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")
        assert Settings(_env_file=None).log_level == "DEBUG"

    def test_monitor_timeout_seconds_override_parses_float(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MONITOR_TIMEOUT_SECONDS", "120.5")
        assert Settings(_env_file=None).monitor_timeout_seconds == 120.5

    def test_monitor_max_polls_override_parses_int(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MONITOR_MAX_POLLS", "42")
        assert Settings(_env_file=None).monitor_max_polls == 42

    def test_default_workflow_timeout_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DEFAULT_WORKFLOW_TIMEOUT", "9000")
        assert Settings(_env_file=None).default_workflow_timeout == 9000.0

    def test_case_insensitive_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # SettingsConfigDict(case_sensitive=False) — uppercase env with lowercase field name.
        monkeypatch.setenv("HOST_ID", "zeus")
        assert Settings(_env_file=None).host_id == "zeus"


class TestRequireTlsParsing:
    """Boolean parsing for REQUIRE_TLS across common truthy/falsy spellings."""

    @pytest.mark.parametrize("value", ["true", "True", "TRUE", "1"])
    def test_truthy_values(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv("REQUIRE_TLS", value)
        assert Settings(_env_file=None).require_tls is True

    @pytest.mark.parametrize("value", ["false", "False", "FALSE", "0"])
    def test_falsy_values(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv("REQUIRE_TLS", value)
        assert Settings(_env_file=None).require_tls is False


class TestClientKwargs:
    """client_kwargs() propagates every AgamemnonClient.__init__ parameter."""

    def test_all_keys_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AGAMEMNON_URL", raising=False)
        monkeypatch.delenv("AGAMEMNON_API_KEY", raising=False)
        monkeypatch.delenv("HOST_ID", raising=False)
        monkeypatch.delenv("REQUIRE_TLS", raising=False)
        monkeypatch.delenv("NATS_URL", raising=False)
        kwargs = Settings(_env_file=None).client_kwargs()
        assert set(kwargs.keys()) == {
            "url",
            "api_key",
            "host_id",
            "require_tls",
            "nats_url",
            "rate_limit_rps",
            "rate_limit_burst",
        }

    def test_url_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGAMEMNON_URL", "https://example.test")
        assert Settings(_env_file=None).client_kwargs()["url"] == "https://example.test"

    def test_api_key_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGAMEMNON_API_KEY", "k-abc")
        assert Settings(_env_file=None).client_kwargs()["api_key"] == "k-abc"

    def test_host_id_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOST_ID", "achilles")
        assert Settings(_env_file=None).client_kwargs()["host_id"] == "achilles"

    def test_nats_url_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NATS_URL", "tls://nats.test:4222")
        assert Settings(_env_file=None).client_kwargs()["nats_url"] == "tls://nats.test:4222"


class TestModuleSingleton:
    """The module-level ``settings`` singleton is instantiable and typed."""

    def test_singleton_exists(self) -> None:
        from telemachy.config import settings as singleton

        assert isinstance(singleton, Settings)

    def test_singleton_client_kwargs_returns_dict(self) -> None:
        from telemachy.config import settings as singleton

        kwargs = singleton.client_kwargs()
        assert isinstance(kwargs, dict)
        assert "url" in kwargs


def test_rate_limit_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGAMEMNON_RATE_LIMIT_RPS", raising=False)
    monkeypatch.delenv("AGAMEMNON_RATE_LIMIT_BURST", raising=False)
    s = Settings(_env_file=None)
    assert s.agamemnon_rate_limit_rps == 0.0
    assert s.agamemnon_rate_limit_burst == 16
    kw = s.client_kwargs()
    assert kw["rate_limit_rps"] == 0.0
    assert kw["rate_limit_burst"] == 16


def test_rate_limit_env_var_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    """AGAMEMNON_RATE_LIMIT_RPS env var actually binds (regression for prior NOGO)."""
    monkeypatch.setenv("AGAMEMNON_RATE_LIMIT_RPS", "12.5")
    monkeypatch.setenv("AGAMEMNON_RATE_LIMIT_BURST", "8")
    s = Settings(_env_file=None)
    assert s.agamemnon_rate_limit_rps == 12.5
    assert s.agamemnon_rate_limit_burst == 8


def test_rate_limit_burst_zero_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """burst=0 must raise ValidationError instead of silent clamp."""
    monkeypatch.setenv("AGAMEMNON_RATE_LIMIT_BURST", "0")
    with pytest.raises(pydantic.ValidationError):
        Settings(_env_file=None)


def test_rate_limit_burst_negative_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGAMEMNON_RATE_LIMIT_BURST", "-5")
    with pytest.raises(pydantic.ValidationError):
        Settings(_env_file=None)
