"""Tests for the Telemachy CLI using typer.testing.CliRunner."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from typer.testing import CliRunner

from telemachy.cli import app
from tests.conftest import make_workflow_dict

runner = CliRunner()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def valid_workflow_file(
    workflow_file_factory: Callable[..., Path],
) -> Path:
    """Write a minimal valid workflow YAML to a tmp file via the shared factory."""
    return workflow_file_factory()


@pytest.fixture()
def invalid_workflow_file(tmp_path: Path) -> Path:
    """Write a workflow YAML missing metadata.name to a tmp file.

    Builds the canonical workflow via the factory, then deletes the name key
    so the only diff from valid_workflow_file is the missing field under test.
    """
    raw = make_workflow_dict()
    del raw["metadata"]["name"]
    p = tmp_path / "invalid_workflow.yaml"
    p.write_text(yaml.safe_dump(raw, sort_keys=False))
    return p


# ---------------------------------------------------------------------------
# TEST 1 — validate command with valid YAML
# ---------------------------------------------------------------------------


def test_validate_valid_yaml(valid_workflow_file: Path) -> None:
    """validate command exits 0 and prints 'valid' for a correct YAML."""
    result = runner.invoke(app, ["validate", str(valid_workflow_file)])
    assert result.exit_code == 0, f"Unexpected exit code. Output:\n{result.output}"
    assert "valid" in result.output.lower(), f"Expected 'valid' in output but got:\n{result.output}"


# ---------------------------------------------------------------------------
# TEST 2 — validate command with invalid YAML (missing name)
# ---------------------------------------------------------------------------


def test_validate_invalid_yaml_missing_name(invalid_workflow_file: Path) -> None:
    """validate command should fail or print an error for a YAML missing metadata.name."""
    result = runner.invoke(app, ["validate", str(invalid_workflow_file)])
    # Either non-zero exit code or error output
    has_error_output = (
        "error" in result.output.lower()
        or "invalid" in result.output.lower()
        or "validation" in result.output.lower()
    )
    assert result.exit_code != 0 or has_error_output, (
        f"Expected failure or error message, got exit_code={result.exit_code} "
        f"output:\n{result.output}"
    )


# ---------------------------------------------------------------------------
# TEST 3 — validate command with non-existent file
# ---------------------------------------------------------------------------


def test_validate_nonexistent_file(tmp_path: Path) -> None:
    """validate command should fail for a file that does not exist."""
    missing = tmp_path / "does_not_exist.yaml"
    result = runner.invoke(app, ["validate", str(missing)])
    assert result.exit_code != 0, (
        f"Expected non-zero exit code for missing file, got {result.exit_code}. "
        f"Output:\n{result.output}"
    )


# ---------------------------------------------------------------------------
# TEST 4 — validate command rejects path with shell metacharacters
# ---------------------------------------------------------------------------


def test_validate_shell_metacharacter_path() -> None:
    """validate command should reject paths containing shell metacharacters."""
    malicious_path = "/tmp/workflow.yaml;rm -rf /"
    result = runner.invoke(app, ["validate", malicious_path])
    assert result.exit_code != 0, (
        f"Expected non-zero exit code for path with metacharacters, "
        f"got {result.exit_code}. Output:\n{result.output}"
    )


def test_validate_pipe_metacharacter_path() -> None:
    """validate command should reject paths containing a pipe character."""
    malicious_path = "/tmp/workflow.yaml|cat /etc/passwd"
    result = runner.invoke(app, ["validate", malicious_path])
    assert result.exit_code != 0, (
        f"Expected non-zero exit code for path with '|', "
        f"got {result.exit_code}. Output:\n{result.output}"
    )


# ---------------------------------------------------------------------------
# TEST 5 — plan command with valid workflow
# ---------------------------------------------------------------------------


def test_plan_valid_workflow(valid_workflow_file: Path) -> None:
    """plan command exits 0 and outputs agent and team names."""
    result = runner.invoke(app, ["plan", str(valid_workflow_file)])
    assert result.exit_code == 0, f"Unexpected exit code. Output:\n{result.output}"
    # Agent name and team name from the fixture YAML should appear
    assert "worker" in result.output, (
        f"Expected agent name 'worker' in plan output:\n{result.output}"
    )
    assert "team-a" in result.output, (
        f"Expected team name 'team-a' in plan output:\n{result.output}"
    )


# ---------------------------------------------------------------------------
# TEST 6 — run command dry-run
# ---------------------------------------------------------------------------


def test_run_dry_run(valid_workflow_file: Path) -> None:
    """run --dry-run exits 0 without connecting to Agamemnon."""
    mock_state = MagicMock()
    mock_state.workflow_id = "dry-run-id-123"
    mock_state.status = "completed"

    with patch("telemachy.cli.run_workflow", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = mock_state
        result = runner.invoke(app, ["run", str(valid_workflow_file), "--dry-run"])

    assert result.exit_code == 0, (
        f"Expected exit code 0 for --dry-run, got {result.exit_code}. Output:\n{result.output}"
    )
    # run_workflow should have been called once with dry_run=True
    mock_run.assert_called_once()
    _args, kwargs = mock_run.call_args
    assert kwargs.get("dry_run") is True or (len(_args) > 1 and _args[1] is True), (
        f"Expected run_workflow to be called with dry_run=True, call_args={mock_run.call_args}"
    )
