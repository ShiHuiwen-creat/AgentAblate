import asyncio
import shutil
import sqlite3
from pathlib import Path
from typing import Annotated

import typer

from agentablate.config import load_experiment
from agentablate.matrix import expand_matrix
from agentablate.reporting import load_report, render_html, render_markdown
from agentablate.runner import TrialRunner
from agentablate.storage import SQLiteStorage
from agentablate.workspace import initialize_fixture_repository

app = typer.Typer(no_args_is_help=True)


@app.callback()
def main() -> None:
    """Benchmark coding-agent skills and MCP servers."""


CONFIG = """version: 1
experiment:
  name: starter
  repetitions: 1
  timeout_seconds: 30
agents:
  - id: fake
    adapter: fake
variants:
  - id: baseline
tasks: [./task.yaml]
"""
TASK = """id: starter-task
repo: ./fixture
revision: HEAD
prompt: Create agentablate-output.txt.
test_command:
  - python3
  - -c
  - \"import pathlib; assert pathlib.Path('agentablate-output.txt').is_file()\"
"""


@app.command()
def init(directory: Path = Path("."), force: bool = False) -> None:
    """Create a starter AgentAblate experiment."""
    targets = (directory / "agentablate.yaml", directory / "task.yaml", directory / "fixture")
    existing = [path.name for path in targets if path.exists()]
    if existing and not force:
        typer.echo(f"Refusing to overwrite; already exists: {', '.join(existing)}")
        raise typer.Exit(1)
    directory.mkdir(parents=True, exist_ok=True)
    if force and (directory / "fixture").exists():
        shutil.rmtree(directory / "fixture")
    (directory / "agentablate.yaml").write_text(CONFIG, encoding="utf-8")
    (directory / "task.yaml").write_text(TASK, encoding="utf-8")
    try:
        initialize_fixture_repository(directory / "fixture")
    except Exception as error:
        typer.echo(f"Initialization failed: {error}")
        raise typer.Exit(1) from error
    typer.echo(f"Initialized experiment in {directory}")


@app.command()
def doctor(config: Path) -> None:
    """Check whether an experiment can run locally."""
    try:
        bundle = load_experiment(config)
    except Exception as error:
        raise typer.BadParameter(str(error), param_hint="config") from error
    unavailable = False
    for agent in bundle.config.agents:
        available = agent.adapter == "fake"
        detail = "built-in fake adapter"
        if agent.adapter == "command":
            executable = agent.command[0] if agent.command else ""
            available = bool(executable and shutil.which(executable))
            detail = executable or "command not configured"
        elif agent.adapter != "fake":
            available = False
            detail = "adapter is not available in Phase 1"
        unavailable |= not available
        typer.echo(f"{agent.id}: {'available' if available else 'unavailable'} ({detail})")
    if unavailable:
        raise typer.Exit(1)


@app.command()
def run(
    config: Path,
    agent: Annotated[list[str] | None, typer.Option("--agent")] = None,
    variant: Annotated[list[str] | None, typer.Option("--variant")] = None,
    task: Annotated[list[str] | None, typer.Option("--task")] = None,
    concurrency: Annotated[int, typer.Option(min=1)] = 1,
    resume: Annotated[bool, typer.Option("--resume/--no-resume")] = True,
) -> None:
    """Run an AgentAblate experiment."""
    try:
        bundle = load_experiment(config)
        trials = expand_matrix(bundle)
    except Exception as error:
        raise typer.BadParameter(str(error), param_hint="config") from error
    selected = [
        trial
        for trial in trials
        if (not agent or trial.agent.id in agent)
        and (not variant or trial.variant.id in variant)
        and (not task or trial.task.id in task)
    ]
    if not selected:
        typer.echo("No trials match the selected filters.")
        raise typer.Exit(1)
    root = bundle.source.parent
    storage = SQLiteStorage(root / ".agentablate" / "results.sqlite3")
    runner = TrialRunner(root, storage, recover_running=resume)
    rows = asyncio.run(runner.run_all(selected, concurrency))
    failed = sum(row.get("status") != "completed" for row in rows)
    typer.echo(f"Ran {len(rows)} trial(s); {failed} failed")
    if failed:
        raise typer.Exit(1)


@app.command()
def compare(database: Path) -> None:
    """Compare experiment variants."""
    try:
        report_data = load_report(database)
    except (OSError, sqlite3.Error) as error:
        raise typer.BadParameter(str(error), param_hint="database") from error
    typer.echo(render_markdown(report_data))


@app.command()
def report(
    database: Path,
    format: Annotated[str, typer.Option("--format")] = "markdown",
    output: Annotated[Path | None, typer.Option("--output")] = None,
) -> None:
    """Generate a static experiment report."""
    if format not in {"markdown", "html"}:
        raise typer.BadParameter("format must be markdown or html", param_hint="format")
    try:
        report_data = load_report(database)
    except (OSError, sqlite3.Error) as error:
        raise typer.BadParameter(str(error), param_hint="database") from error
    suffix = "md" if format == "markdown" else "html"
    destination = output or Path(f"agentablate-report.{suffix}")
    rendered = render_markdown(report_data) if format == "markdown" else render_html(report_data)
    destination.write_text(rendered, encoding="utf-8")
    typer.echo(f"Wrote {destination}")
