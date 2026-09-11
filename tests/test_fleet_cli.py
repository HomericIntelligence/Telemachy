"""Explicit Fleet CLI boundary, including a completely offline preview."""

import json
from collections.abc import Callable
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from typer.testing import CliRunner

from telemachy.cli import app
from tests.test_fleet_registration import FakeGitHub


def arguments(path: Path) -> list[str]:
    return [
        "register-fleet-epic",
        str(path),
        "--repo",
        "Homeric/repo",
        "--epic-issue",
        "42",
        "--registration-key",
        "reviewed-idea",
    ]


def test_preview_is_offline_and_describes_existing_epic_opt_in(
    workflow_file_factory: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with (
        patch("httpx.AsyncClient", side_effect=AssertionError("must remain offline")),
        patch("nats.connect", side_effect=AssertionError("must remain offline")),
    ):
        result = CliRunner().invoke(app, arguments(workflow_file_factory()) + ["--dry-run"])
    assert result.exit_code == 0, result.output
    preview = json.loads(result.stdout)
    assert preview["epic"] == 42
    assert preview["phase"] == "preview"
    assert preview["createsEpic"] is False
    assert "key=reviewed-idea" in preview["requiredMarker"]


def test_registration_requires_explicit_single_writer_configuration(
    workflow_file_factory: Callable[..., Path],
) -> None:
    result = CliRunner().invoke(app, arguments(workflow_file_factory()))
    assert result.exit_code == 1
    assert "exclusive_writer_required" in result.output


def test_registration_requires_backend_github_credential(
    workflow_file_factory: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    result = CliRunner().invoke(app, arguments(workflow_file_factory()) + ["--exclusive-writer"])
    assert result.exit_code == 1
    assert "github_token_required" in result.output


def test_cli_uses_durable_gateway_and_puback_result(
    workflow_file_factory: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "fixture-private-token")
    monkeypatch.setenv("NATS_CLIENT_TOKEN", "fixture-broker-token")
    monkeypatch.delenv("NATS_TOKEN", raising=False)
    gh = FakeGitHub()

    class Gateway:
        async def __aenter__(self) -> FakeGitHub:
            return gh

        async def __aexit__(self, *args: object) -> None:
            pass

    publisher = AsyncMock(
        return_value={"stream": "homeric-pipeline", "seq": 17, "duplicate": False}
    )
    with (
        patch("telemachy.fleet_cli.GitHubIssueGateway") as factory,
        patch("telemachy.fleet_cli.publish_registration", publisher),
        patch("telemachy.fleet_cli.settings.nats_url", "tls://fixture.invalid:4222"),
    ):
        factory.return_value = Gateway()
        result = CliRunner().invoke(
            app, arguments(workflow_file_factory()) + ["--exclusive-writer"]
        )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["receipt"]["seq"] == 17
    assert publisher.await_count == 1
    assert publisher.await_args.kwargs["token"] == "fixture-broker-token"
    assert "fixture-private-token" not in result.output
