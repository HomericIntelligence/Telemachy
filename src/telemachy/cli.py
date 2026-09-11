"""Typer CLI for Telemachy."""

from __future__ import annotations

import asyncio
import logging
import re
import signal
import uuid
from pathlib import Path
from typing import Annotated

import typer
import yaml
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from telemachy.agamemnon_client import AgamemnonClient
from telemachy.config import settings
from telemachy.executor import WorkflowExecutor, run_workflow
from telemachy.fleet_cli import register_fleet_epic_cmd
from telemachy.models import WorkflowSpec

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


_SHELL_METACHARACTERS: re.Pattern[str] = re.compile(r"[;&|$`><(){}\[\]!?*~\\]")


def _validate_workflow_path(path: Path) -> None:
    """Validate that *path* is safe to use as a workflow file path.

    Raises :class:`typer.BadParameter` if the path string contains shell
    metacharacters, or if the resolved path does not point to an existing file.
    """
    raw = str(path)
    if _SHELL_METACHARACTERS.search(raw):
        raise typer.BadParameter(f"Workflow path contains disallowed shell metacharacters: {raw!r}")
    if not path.exists():
        raise typer.BadParameter(f"Workflow file not found: {raw!r}")
    if not path.is_file():
        raise typer.BadParameter(f"Workflow path is not a file: {raw!r}")


def _load_workflow(workflow_path: Path) -> WorkflowSpec:
    """Parse and validate a workflow YAML file into a WorkflowSpec."""
    if not workflow_path.exists():
        err_console.print(f"[red]File not found:[/red] {workflow_path}")
        raise typer.Exit(1)

    try:
        raw = yaml.safe_load(workflow_path.read_text())
    except yaml.YAMLError as exc:
        err_console.print(f"[red]YAML parse error:[/red] {exc}")
        raise typer.Exit(1) from exc

    try:
        return WorkflowSpec.model_validate(raw)
    except Exception as exc:
        err_console.print(f"[red]Workflow schema error:[/red] {exc}")
        raise typer.Exit(1) from exc


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
                from telemachy.audit import build_sink_from_settings
                from telemachy.state_store import FileStateStore

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

                store = FileStateStore(settings.state_dir)
                sink = build_sink_from_settings()
                workflow_id = str(uuid.uuid4())[:8]
                console.print(f"[dim]workflow id:[/dim] {workflow_id}")

                async def _watch_cancel() -> None:
                    while not stop_event.is_set():
                        if store.is_cancel_requested(workflow_id):
                            logger.warning("Cancel sentinel detected for %s", workflow_id)
                            stop_event.set()
                            return
                        await asyncio.sleep(1.0)

                watcher = asyncio.create_task(_watch_cancel())
                try:
                    executor = WorkflowExecutor(
                        client,
                        stop_event=stop_event,
                        force=force,
                        existing_snapshot=snapshot,
                        state_writer=store.save,
                        sink=sink,
                    )
                    executor.add_hook("on_task_complete", _on_task_complete)
                    result = await executor.execute(spec, workflow_id=workflow_id)
                finally:
                    watcher.cancel()
                    # Wait for the cancelled watcher to finish unwinding;
                    # CancelledError is expected and swallowed by gather.
                    await asyncio.gather(watcher, return_exceptions=True)
                    store.clear_cancel(workflow_id)

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
def status(
    workflow_id: Annotated[str, typer.Argument(help="Workflow ID returned by 'run'")],
) -> None:
    """Show the status of a workflow."""
    from telemachy.state_store import (
        CorruptStateError,
        FileStateStore,
        WorkflowNotFoundError,
    )

    store = FileStateStore(settings.state_dir)
    try:
        state = store.load(workflow_id)
    except WorkflowNotFoundError:
        err_console.print(f"[red]Workflow not found:[/red] {workflow_id}")
        raise typer.Exit(1) from None
    except CorruptStateError as exc:
        err_console.print(f"[red]State file is corrupt:[/red] {exc}")
        raise typer.Exit(1) from None
    table = Table(title=f"Workflow {state.workflow_id}")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("name", state.spec.name)
    table.add_row("status", state.status)
    table.add_row("started_at", state.started_at or "-")
    table.add_row("completed_at", state.completed_at or "-")
    table.add_row("error", state.error or "-")
    console.print(table)


@app.command(name="list")
def list_workflows() -> None:
    """List all workflows recorded in the state directory."""
    from telemachy.state_store import FileStateStore

    store = FileStateStore(settings.state_dir)
    states = store.list()
    if not states:
        console.print("[dim]No workflows recorded.[/dim]")
        return
    table = Table(title="Workflows")
    for col in ("ID", "Name", "Status", "Started"):
        table.add_column(col)
    for s in states:
        table.add_row(s.workflow_id, s.spec.name, s.status, s.started_at or "-")
    console.print(table)


@app.command()
def cancel(
    workflow_id: Annotated[str, typer.Argument(help="Workflow ID to cancel")],
) -> None:
    """Request cancellation of a running workflow."""
    from telemachy.state_store import (
        CorruptStateError,
        FileStateStore,
        WorkflowNotFoundError,
    )

    store = FileStateStore(settings.state_dir)
    try:
        state = store.request_cancel(workflow_id)
    except WorkflowNotFoundError:
        err_console.print(f"[red]Workflow not found:[/red] {workflow_id}")
        raise typer.Exit(1) from None
    except CorruptStateError as exc:
        err_console.print(f"[red]State file is corrupt:[/red] {exc}")
        raise typer.Exit(1) from None
    if state.status in {"completed", "failed", "cancelled"}:
        console.print(
            f"[yellow]Workflow {workflow_id} already {state.status} — nothing to cancel.[/yellow]"
        )
        return
    console.print(
        f"[green]Cancellation requested for {workflow_id}.[/green] "
        f"(detected within ~1s, applied within ~6s)"
    )


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


@app.command(name="register-epic")
def register_epic_cmd(
    workflow_path: Annotated[Path, typer.Argument(help="Path to workflow YAML file")],
    repo: Annotated[
        str | None,
        typer.Option("--repo", help="Target GitHub repo OWNER/NAME (default: current repo)"),
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Plan only: create nothing, publish nothing")
    ] = False,
) -> None:
    """Describe a workflow as a GitHub epic + child issues and publish the trigger.

    Creates one child issue per task (label ``state:needs-plan``), an epic
    issue with the parseable task-list body (label ``agamemnon-epic``), and
    publishes ``hi.pipeline.epic.{key}.registered`` (Odysseus ADR-013 §6).
    The final stdout line is a JSON result object — a machine contract for
    callers (e.g. the Hephaestus research myrmidon).
    """
    import json as _json

    from telemachy.github_epic import register_epic

    _validate_workflow_path(workflow_path)
    spec = _load_workflow(workflow_path)
    try:
        result = register_epic(spec, repo=repo, nats_url=settings.nats_url, dry_run=dry_run)
    except Exception as exc:
        err_console.print(f"[bold red]register-epic failed:[/bold red] {exc}")
        raise typer.Exit(1) from exc
    # Human-readable summary on stderr-adjacent rich console; JSON contract on
    # plain stdout as the FINAL line.
    if not dry_run:
        console.print(
            f"[bold green]Epic #{result['epic']} registered on {result['repo']}"
            f"[/bold green] (key {result['key']}, {len(result['children'])} children)"
        )
    print(_json.dumps(result))


def main() -> None:
    _setup_logging()
    app()


if __name__ == "__main__":
    main()
