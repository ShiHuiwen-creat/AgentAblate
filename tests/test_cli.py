import json
import os
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from typer.testing import CliRunner

from agentablate.cli import app


class IsolatedCliRunner(CliRunner):
    @contextmanager
    def isolated_filesystem(self) -> Iterator[str]:
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as directory:
            os.chdir(directory)
            try:
                yield directory
            finally:
                os.chdir(previous)


runner = IsolatedCliRunner()


def test_cli_help_lists_primary_commands() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    for command in ("init", "doctor", "run", "compare", "report"):
        assert command in result.stdout


def test_init_creates_runnable_git_fixture_and_requires_force() -> None:
    with runner.isolated_filesystem():
        first = runner.invoke(app, ["init"])
        root = Path.cwd()

        assert first.exit_code == 0
        assert (root / "agentablate.yaml").is_file()
        assert (root / "task.yaml").is_file()
        assert (root / "fixture" / ".git").is_dir()
        original = (root / "task.yaml").read_text()

        refused = runner.invoke(app, ["init"])
        assert refused.exit_code != 0
        assert "already exists" in refused.stdout
        assert (root / "task.yaml").read_text() == original

        (root / "task.yaml").write_text("changed")
        forced = runner.invoke(app, ["init", "--force"])
        assert forced.exit_code == 0
        assert (root / "task.yaml").read_text() == original


def test_doctor_reports_every_agent_and_fails_for_missing_command() -> None:
    with runner.isolated_filesystem():
        assert runner.invoke(app, ["init"]).exit_code == 0
        config = Path("agentablate.yaml")
        config.write_text(
            config.read_text().replace(
                "variants:",
                "  - id: missing\n    adapter: command\n"
                "    command: [definitely-not-an-agentablate-command]\nvariants:",
            )
        )

        result = runner.invoke(app, ["doctor", str(config)])

        assert result.exit_code == 1
        assert "fake" in result.stdout and "available" in result.stdout
        assert "missing" in result.stdout and "unavailable" in result.stdout


def test_run_filters_trials_and_writes_database_and_jsonl() -> None:
    with runner.isolated_filesystem():
        assert runner.invoke(app, ["init"]).exit_code == 0
        config = Path("agentablate.yaml")
        config.write_text(
            config.read_text().replace("- id: baseline", "- id: baseline\n  - id: extra")
        )

        result = runner.invoke(
            app,
            ["run", str(config), "--variant", "extra", "--concurrency", "2", "--no-resume"],
        )

        assert result.exit_code == 0, result.stdout
        database = Path(".agentablate/results.sqlite3")
        assert database.is_file()
        with closing(sqlite3.connect(database)) as connection:
            rows = connection.execute("SELECT variant_id, status FROM trials").fetchall()
        assert rows == [("extra", "completed")]
        event_files = list(Path(".agentablate/runs").glob("*/events.jsonl"))
        assert len(event_files) == 1
        assert json.loads(event_files[0].read_text().splitlines()[0])["trial_id"]


def test_run_fails_clearly_when_filters_match_nothing() -> None:
    with runner.isolated_filesystem():
        assert runner.invoke(app, ["init"]).exit_code == 0

        result = runner.invoke(app, ["run", "agentablate.yaml", "--agent", "unknown"])

        assert result.exit_code != 0
        assert "no trials match" in result.stdout.lower()


def test_compare_and_report_end_to_end() -> None:
    with runner.isolated_filesystem():
        assert runner.invoke(app, ["init"]).exit_code == 0
        config = Path("agentablate.yaml")
        config.write_text(
            config.read_text().replace("- id: baseline", "- id: baseline\n  - id: extra")
        )
        assert runner.invoke(app, ["run", str(config)]).exit_code == 0
        database = ".agentablate/results.sqlite3"

        comparison = runner.invoke(app, ["compare"])
        markdown = runner.invoke(app, ["report", "--format", "markdown"])
        html = runner.invoke(app, ["report", database, "--format", "html", "--output", "out.html"])

        assert comparison.exit_code == 0
        assert "baseline" in comparison.stdout and "extra" in comparison.stdout
        assert "success" in comparison.stdout.lower()
        assert markdown.exit_code == 0 and Path("agentablate-report.md").is_file()
        assert html.exit_code == 0 and Path("out.html").is_file()
        assert "<style>" in Path("out.html").read_text()
        assert "http" not in Path("out.html").read_text()

        first = Path("agentablate-report.md").read_bytes()
        assert runner.invoke(app, ["report", "--format", "markdown"]).exit_code == 0
        assert Path("agentablate-report.md").read_bytes() == first


def test_default_config_and_resume_recover_stale_running_trial() -> None:
    with runner.isolated_filesystem():
        assert runner.invoke(app, ["init"]).exit_code == 0
        assert runner.invoke(app, ["doctor"]).exit_code == 0
        assert runner.invoke(app, ["run"]).exit_code == 0
        database = Path(".agentablate/results.sqlite3")
        stale = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("UPDATE trials SET status='running', heartbeat_at=?", (stale,))

        blocked = runner.invoke(app, ["run", "--no-resume"])
        recovered = runner.invoke(app, ["run", "--resume"])

        assert blocked.exit_code == 1
        assert recovered.exit_code == 0
        with closing(sqlite3.connect(database)) as connection:
            assert connection.execute("SELECT status FROM trials").fetchone()[0] == "completed"


def test_combined_agent_variant_and_task_filters() -> None:
    with runner.isolated_filesystem():
        assert runner.invoke(app, ["init"]).exit_code == 0
        config = Path("agentablate.yaml")
        config.write_text(
            config.read_text()
            .replace(
                "- id: fake\n    adapter: fake",
                "- id: fake\n    adapter: fake\n  - id: second\n    adapter: fake",
            )
            .replace("- id: baseline", "- id: baseline\n  - id: extra")
        )

        result = runner.invoke(
            app,
            ["run", "--agent", "second", "--variant", "extra", "--task", "starter-task"],
        )

        assert result.exit_code == 0, (result.stdout, result.exception)
        with closing(sqlite3.connect(".agentablate/results.sqlite3")) as connection:
            row = connection.execute("SELECT agent_id, variant_id, task_id FROM trials").fetchone()
        assert row == ("second", "extra", "starter-task")


def test_io_errors_are_friendly_without_traceback() -> None:
    with runner.isolated_filesystem():
        result = runner.invoke(app, ["report", "--output", "missing/out.md"])

        assert result.exit_code == 1
        assert "Traceback" not in result.stdout


def test_malformed_yaml_is_a_friendly_configuration_error() -> None:
    with runner.isolated_filesystem():
        Path("agentablate.yaml").write_text("agents: [", encoding="utf-8")

        result = runner.invoke(app, ["doctor"])

        assert result.exit_code == 1
        assert "Traceback" not in result.stdout
        assert "invalid YAML" in result.stdout


def test_empty_test_command_is_a_friendly_configuration_error() -> None:
    with runner.isolated_filesystem():
        assert runner.invoke(app, ["init"]).exit_code == 0
        task = Path("task.yaml")
        task.write_text(
            """
id: starter-task
repo: ./fixture
prompt: Test the project.
test_command: []
""".strip()
        )

        result = runner.invoke(app, ["doctor"])

        assert result.exit_code == 1
        assert "test_command" in result.stdout
        assert "Traceback" not in result.stdout


def test_report_output_error_uses_application_error_boundary() -> None:
    with runner.isolated_filesystem():
        assert runner.invoke(app, ["init"]).exit_code == 0
        assert runner.invoke(app, ["run"]).exit_code == 0
        Path("blocked").write_text("not a directory", encoding="utf-8")

        result = runner.invoke(app, ["report", "--output", "blocked/report.md"])

        assert result.exit_code == 1
        assert "Traceback" not in result.stdout
