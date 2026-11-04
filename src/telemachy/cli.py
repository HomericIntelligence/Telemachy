"""Typer CLI for Telemachy."""

from __future__ import annotations

import asyncio
import logging
import signal
import uuid
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from telemachy.agamemnon_client import AgamemnonClient
from telemachy.config import settings
from telemachy.executor import WorkflowExecutor, run_workflow
from telemachy.fleet_cli import register_fleet_epic_cmd
from telemachy.models import WorkflowSpec
from telemachy.workflow_input import load_workflow as _load_workflow
from telemachy.workflow_input import validate_workflow_path as _validate_workflow_path

app = typer.Typer(
    name="telemachy",
    help="Declarative workflow engine for ProjectAgamemnon.",
    add_completion=False,
)
console = Console()
err_console = Console(stderr=True)
app.command(name="register-fleet-epic")(register_fleet_epic_cmd)

logger = logging.getLogger(__name__)


def _setup_logging() -> None:
    """Configure logging from settings (LOG_LEVEL, LOG_FORMAT env vars).

    Sets up correlation ID injection, JSON or plain formatters, and optionally
    starts OpenTelemetry tracing and Prometheus metrics (before any httpx clients
    are instantiated so HTTPXClientInstrumentor patches the class).
    """
    from telemachy.telemetry import (
        JsonFormatter,
        SafePlainFormatter,
        TelemachyDefaultHandler,
        WorkflowContextLogFilter,
        setup_metrics,
        setup_tracing,
    )

    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    root = logging.getLogger()

    # Remove only handlers Telemachy itself installed — caplog and
    # library-installed handlers are preserved.
    for h in list(root.handlers):
        if isinstance(h, TelemachyDefaultHandler):
            root.removeHandler(h)

    handler = TelemachyDefaultHandler()
    handler.addFilter(WorkflowContextLogFilter())
    if settings.log_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            SafePlainFormatter(
                "%(asctime)s %(levelname)s [wf=%(workflow_id)s] %(name)s %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            )
        )
    root.setLevel(level)
    root.addHandler(handler)

    # OTel MUST be set up BEFORE httpx clients are instantiated so the
    # HTTPXClientInstrumentor patches the class before any AsyncClient
    # is created. Both clients in this codebase are created inside
    # async functions called after _setup_logging() runs, so this
    # ordering is satisfied by construction (cli.run → asyncio.run →
    # AgamemnonClient context manager).
    if settings.otel_enabled:
        setup_tracing(settings.otel_service_name)
    if settings.metrics_enabled:
        setup_metrics(settings.metrics_port)


def _print_plan(spec: WorkflowSpec) -> None:
    """Print a human-readable plan of what would be created."""
    console.print(
        Panel(
            f"[bold]{spec.name}[/bold]\n{spec.description}",
            title="Workflow Plan",
            border_style="blue",
        )
    )

    # Agents table
    agent_table = Table(title="Agents to provision", show_header=True)
    agent_table.add_column("Name")
    agent_table.add_column("Program")
    agent_table.add_column("Runtime")
    agent_table.add_column("Image / Model")
    for agent in spec.agents:
        extra = agent.docker_image or agent.model or "-"
        agent_table.add_row(agent.name, agent.program, agent.runtime, extra)
    console.print(agent_table)

    # Tasks per team
    for team in spec.teams:
        task_table = Table(title=f"Team: {team.name} — tasks", show_header=True)
        task_table.add_column("Title")
        task_table.add_column("Assign to")
        task_table.add_column("Depends on")
        for task in team.tasks:
            task_table.add_row(
                task.subject,
                task.assign_to,
                ", ".join(task.blocked_by) or "-",
            )
        console.print(task_table)

    console.print(f"[dim]Teardown policy:[/dim] {spec.teardown}")


@app.command()
def run(
    workflow_path: Annotated[
        Path,
        typer.Argument(
            help=f"Path to workflow YAML file (default search dir: {settings.workflows_dir}, override with WORKFLOWS_DIR env var)"
        ),
    ],
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run/--no-dry-run", help="Simulate execution without calling Agamemnon"),
    ] = False,
    force: Annotated[
        bool,
        typer.Option(
            "--force/--no-force",
            help="Bypass idempotency lookup and create fresh agents/teams. "
            "Pre-existing tlm-* resources from prior runs are NOT deleted.",
        ),
    ] = False,
) -> None:
    """Execute a workflow YAML file."""
    _validate_workflow_path(workflow_path)
    spec = _load_workflow(workflow_path)

    if dry_run:
        console.print(f"[bold yellow][dry-run][/bold yellow] Simulating workflow: {spec.name}")
        _print_plan(spec)
        state = asyncio.run(run_workflow(spec, dry_run=True, force=force))
        console.print(
            f"[bold yellow][dry-run][/bold yellow] Simulation complete. id={state.workflow_id}"
        )
        return

    console.print(f"[bold green]Running workflow:[/bold green] {spec.name}")

    # Count total tasks across all teams for the progress display
    total_tasks = sum(len(team.tasks) for team in spec.teams)

    async def _run_with_signals() -> None:
        from telemachy.idempotency import make_key

        stop_event = asyncio.Event()
        completed_count = 0

        def _handle_signal(sig: int, _frame: object) -> None:
            logger.warning("Received signal %s, initiating graceful shutdown...", sig)
            stop_event.set()

        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
            transient=True,
        ) as progress:
            task_id = progress.add_task(
                f"Running workflow: {spec.name}  [Completed: 0/{total_tasks}]",
                total=None,
            )

            def _on_task_complete(**kwargs: object) -> None:
                nonlocal completed_count
                completed_count += 1
                task_info = kwargs.get("task", {})
                subject = task_info.get("subject", "?") if isinstance(task_info, dict) else "?"
                progress.update(
                    task_id,
                    description=(
                        f"Running workflow: {spec.name}  "
                        f"[Completed: {completed_count}/{total_tasks}]  "
                        f"last: {subject}"
                    ),
                )

            async with AgamemnonClient(**settings.client_kwargs()) as client:
                # Take ONE snapshot, use it for both the --force warning AND
                # the executor's lookup tables (avoid two round-trips).
                snapshot: tuple[list, list] | None = None
                if not force:
                    snapshot = (await client.list_agents(), await client.list_teams())
                else:
                    agents_now = await client.list_agents()
                    teams_now = await client.list_teams()
                    expected = {make_key(spec.name, a.name) for a in spec.agents} | {
                        make_key(spec.name, t.name) for t in spec.teams
                    }
                    matched = [a for a in agents_now if str(a.get("name", "")) in expected]
                    matched += [t for t in teams_now if str(t.get("name", "")) in expected]
                    if matched:
                        err_console.print(
                            "[yellow]--force: bypassing idempotency; "
                            f"{len(matched)} pre-existing tlm-* resource(s) for "
                            f"workflow '{spec.name}' will be left behind. "
                            "Clean up manually if desired.[/yellow]"
                        )

                workflow_id = str(uuid.uuid4())[:8]
                console.print(f"[dim]workflow id:[/dim] {workflow_id}")

                executor = WorkflowExecutor(
                    client,
                    stop_event=stop_event,
                    force=force,
                    existing_snapshot=snapshot,
                )
                executor.add_hook("on_task_complete", _on_task_complete)
                result = await executor.execute(spec, workflow_id=workflow_id)

        if result.status == "completed":
            console.print(f"[bold green]Workflow completed.[/bold green] id={result.workflow_id}")
        else:
            console.print(
                f"[bold red]Workflow {result.status}.[/bold red] "
                f"id={result.workflow_id}" + (f"  error={result.error}" if result.error else "")
            )
            raise typer.Exit(1)

    asyncio.run(_run_with_signals())


@app.command()
def plan(
    workflow_path: Annotated[Path, typer.Argument(help="Path to workflow YAML file")],
) -> None:
    """Dry-run: print what would be created without executing."""
    _validate_workflow_path(workflow_path)
    spec = _load_workflow(workflow_path)
    _print_plan(spec)


@app.command()
def validate(
    workflow_path: Annotated[
        Path,
        typer.Argument(
            help=f"Path to workflow YAML file (default search dir: {settings.workflows_dir}, override with WORKFLOWS_DIR env var)"
        ),
    ],
) -> None:
    """Validate a workflow YAML file against the Telemachy schema."""
    _validate_workflow_path(workflow_path)
    spec = _load_workflow(workflow_path)
    console.print(f"[bold green]Valid.[/bold green] Workflow: {spec.name}")


@app.command()
def schema(
    output: Path = typer.Option(  # noqa: B008
        Path("schemas/workflow-v1.json"),
        "--output",
        "-o",
        help="Path to write the JSON Schema file",
    ),
) -> None:
    """Export the workflow YAML JSON Schema for editor validation."""
    from telemachy.schema import write_workflow_schema

    output.parent.mkdir(parents=True, exist_ok=True)
    write_workflow_schema(output)
    typer.echo(f"Schema written to {output}")


def main() -> None:
    _setup_logging()
    app()


if __name__ == "__main__":
    main()
