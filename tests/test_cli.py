from typer.testing import CliRunner

from agentablate.cli import app

runner = CliRunner()


def test_cli_help_lists_primary_commands() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    for command in ("init", "doctor", "run", "compare", "report"):
        assert command in result.stdout
