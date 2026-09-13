"""Shared workflow-file input for legacy and Fleet commands."""

from __future__ import annotations

import re
from pathlib import Path

import typer
import yaml
from rich.console import Console

from telemachy.models import WorkflowSpec

_SHELL_METACHARACTERS: re.Pattern[str] = re.compile(r"[;&|$`><(){}\[\]!?*~\\]")
_ERR_CONSOLE = Console(stderr=True)


def validate_workflow_path(path: Path) -> None:
    """Reject missing files and shell metacharacters before workflow parsing."""
    raw = str(path)
    if _SHELL_METACHARACTERS.search(raw):
        raise typer.BadParameter(f"Workflow path contains disallowed shell metacharacters: {raw!r}")
    if not path.exists():
        raise typer.BadParameter(f"Workflow file not found: {raw!r}")
    if not path.is_file():
        raise typer.BadParameter(f"Workflow path is not a file: {raw!r}")


def load_workflow(workflow_path: Path) -> WorkflowSpec:
    """Parse and validate a workflow YAML file into a WorkflowSpec."""
    if not workflow_path.exists():
        _ERR_CONSOLE.print(f"[red]File not found:[/red] {workflow_path}")
        raise typer.Exit(1)
    try:
        raw = yaml.safe_load(workflow_path.read_text())
    except yaml.YAMLError as exc:
        _ERR_CONSOLE.print(f"[red]YAML parse error:[/red] {exc}")
        raise typer.Exit(1) from exc
    try:
        return WorkflowSpec.model_validate(raw)
    except Exception as exc:
        _ERR_CONSOLE.print(f"[red]Workflow schema error:[/red] {exc}")
        raise typer.Exit(1) from exc
