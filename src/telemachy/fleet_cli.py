"""Explicit Fleet registration command; legacy registration stays separate."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlparse

import typer

from telemachy.config import settings
from telemachy.fleet_github import GitHubIssueGateway
from telemachy.fleet_publish import publish_registration
from telemachy.fleet_registration import (
    RegistrationError,
    canonical,
    register_fleet_epic,
    registration_template,
)
from telemachy.workflow_input import load_workflow, validate_workflow_path


def register_fleet_epic_cmd(
    workflow_path: Annotated[Path, typer.Argument(help="Reviewed workflow YAML file")],
    repo: Annotated[str, typer.Option("--repo", help="Canonical GitHub OWNER/NAME")],
    epic_issue: Annotated[
        int, typer.Option("--epic-issue", min=1, help="Existing epic issue number")
    ],
    registration_key: Annotated[
        str, typer.Option("--registration-key", help="Existing epic marker key")
    ],
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Offline preview; no network or mutations")
    ] = False,
    exclusive_writer: Annotated[
        bool,
        typer.Option(
            "--exclusive-writer",
            help="Assert that deployment grants this process sole registration ownership",
        ),
    ] = False,
) -> None:
    """Register into an opted-in existing epic with GitHub progress and JetStream PubAck.

    This does not create the first epic or execute workflow tasks. Only one
    writer may register a given epic; GitHub issue PATCH is not an atomic lock.
    """
    validate_workflow_path(workflow_path)
    spec = load_workflow(workflow_path)
    try:
        marker = registration_template(registration_key)
        GitHubIssueGateway._path(repo, epic_issue)
        if dry_run:
            typer.echo(
                canonical(
                    {
                        "schema": "hi/telemachy/fleet-registration-preview/v1",
                        "repo": repo.lower(),
                        "epic": epic_issue,
                        "phase": "preview",
                        "createsEpic": False,
                        "requiredMarker": marker,
                        "tasks": sum(len(team.tasks) for team in spec.teams),
                    }
                )
            )
            return
        if not exclusive_writer:
            raise RegistrationError("exclusive_writer_required")
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            raise RegistrationError("github_token_required")
        if settings.require_tls and urlparse(settings.nats_url).scheme != "tls":
            raise RegistrationError("fleet_nats_tls_required")

        async def submit() -> dict[str, Any]:
            async def publish(subject: str, payload: dict[str, Any]) -> dict[str, Any]:
                return await publish_registration(
                    subject,
                    payload,
                    settings.nats_url,
                    token=os.environ.get("NATS_CLIENT_TOKEN"),
                    allow_insecure=not settings.require_tls,
                )

            async with GitHubIssueGateway(token) as github:
                return await register_fleet_epic(
                    spec,
                    repo=repo,
                    epic_issue=epic_issue,
                    registration_key=registration_key,
                    github=github,
                    publish=publish,
                )

        typer.echo(canonical(asyncio.run(submit())))
    except Exception as exc:
        # Transport exceptions can embed credentials or request bodies. Only
        # our stable error codes cross the CLI boundary; original cause stays private.
        code = str(exc) if isinstance(exc, RegistrationError) else "registration_unconfirmed"
        typer.echo(code, err=True)
        raise typer.Exit(1) from None
