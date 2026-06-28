from pathlib import Path

import typer

app = typer.Typer(no_args_is_help=True)


@app.callback()
def main() -> None:
    """Benchmark coding-agent skills and MCP servers."""


@app.command()
def init(directory: Path = Path(".")) -> None:
    """Create a starter AgentAblate experiment."""


@app.command()
def doctor(config: Path) -> None:
    """Check whether an experiment can run locally."""


@app.command()
def run(config: Path) -> None:
    """Run an AgentAblate experiment."""


@app.command()
def compare(database: Path) -> None:
    """Compare experiment variants."""


@app.command()
def report(database: Path) -> None:
    """Generate a static experiment report."""
