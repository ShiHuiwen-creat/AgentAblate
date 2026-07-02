from pathlib import Path
from typing import Annotated

import typer

from agentablate.experiment import (
    ApplicationError,
    compare_results,
    doctor_experiment,
    initialize_experiment,
    run_experiment,
    starter_documents,
    write_report,
)
from agentablate.reporting import render_comparison

app = typer.Typer(no_args_is_help=True)


@app.callback()
def main() -> None:
    """Benchmark coding-agent skills and MCP servers."""


@app.command()
def init(directory: Path = Path("."), force: bool = False) -> None:
    """Create a starter AgentAblate experiment."""
    try:
        config_text, task_text = starter_documents()
        initialize_experiment(directory, config_text, task_text, force)
    except ApplicationError as error:
        typer.echo(str(error))
        raise typer.Exit(1) from error
    typer.echo(f"Initialized experiment in {directory}")


@app.command()
def doctor(
    config: Annotated[Path, typer.Argument()] = Path("agentablate.yaml"),
) -> None:
    """Check whether an experiment can run locally."""
    try:
        results = doctor_experiment(config)
    except ApplicationError as error:
        typer.echo(str(error))
        raise typer.Exit(1) from error
    for result in results:
        typer.echo(
            f"{result.agent_id}: "
            f"{'available' if result.available else 'unavailable'} ({result.detail})"
        )
    if any(not result.available for result in results):
        raise typer.Exit(1)


@app.command()
def run(
    config: Annotated[Path, typer.Argument()] = Path("agentablate.yaml"),
    agent: Annotated[list[str] | None, typer.Option("--agent")] = None,
    variant: Annotated[list[str] | None, typer.Option("--variant")] = None,
    task: Annotated[list[str] | None, typer.Option("--task")] = None,
    concurrency: Annotated[int, typer.Option(min=1)] = 1,
    resume: Annotated[bool, typer.Option("--resume/--no-resume")] = True,
) -> None:
    """Run an AgentAblate experiment."""
    try:
        summary = run_experiment(
            config,
            agents=agent or (),
            variants=variant or (),
            tasks=task or (),
            concurrency=concurrency,
            resume=resume,
        )
    except ApplicationError as error:
        typer.echo(str(error))
        raise typer.Exit(1) from error
    typer.echo(f"Ran {summary.total} trial(s); {summary.failed} failed")
    if summary.failed:
        raise typer.Exit(1)


@app.command()
def compare(
    database: Annotated[Path, typer.Argument()] = Path(".agentablate/results.sqlite3"),
) -> None:
    """Compare experiment variants."""
    try:
        comparison = compare_results(database)
    except ApplicationError as error:
        typer.echo(str(error))
        raise typer.Exit(1) from error
    typer.echo(render_comparison(comparison))


@app.command()
def report(
    database: Annotated[Path, typer.Argument()] = Path(".agentablate/results.sqlite3"),
    format: Annotated[str, typer.Option("--format")] = "markdown",
    output: Annotated[Path | None, typer.Option("--output")] = None,
) -> None:
    """Generate a static experiment report."""
    if format not in {"markdown", "html"}:
        raise typer.BadParameter("format must be markdown or html", param_hint="format")
    try:
        suffix = "md" if format == "markdown" else "html"
        destination = output or Path(f"agentablate-report.{suffix}")
        write_report(database, destination, format)
    except ApplicationError as error:
        typer.echo(str(error))
        raise typer.Exit(1) from error
    typer.echo(f"Wrote {destination}")
