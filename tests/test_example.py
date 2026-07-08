import shutil
from pathlib import Path

from typer.testing import CliRunner

from agentablate.cli import app
from agentablate.workspace import initialize_fixture_repository


def test_fake_ablation_example_runs_and_reports_both_variants(
    tmp_path: Path, monkeypatch
) -> None:
    source = Path(__file__).parents[1] / "examples" / "fake-ablation"
    example = tmp_path / "fake-ablation"
    shutil.copytree(source, example)
    initialize_fixture_repository(example / "fixture")
    monkeypatch.chdir(example)

    runner = CliRunner()
    run_result = runner.invoke(app, ["run"])
    markdown_result = runner.invoke(app, ["report", "--format", "markdown"])
    html_result = runner.invoke(
        app,
        ["report", "--format", "html", "--output", "agentablate-report.html"],
    )

    assert run_result.exit_code == 0, run_result.output
    assert markdown_result.exit_code == 0, markdown_result.output
    assert html_result.exit_code == 0, html_result.output
    for report in ("agentablate-report.md", "agentablate-report.html"):
        text = (example / report).read_text(encoding="utf-8")
        assert "baseline" in text
        assert "with-skill" in text
