import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from zipfile import ZipFile

from typer.testing import CliRunner

from agentablate.cli import app
from agentablate.config import load_experiment
from agentablate.reporting import load_comparison, load_report
from agentablate.skills import inspect_skill
from agentablate.workspace import initialize_fixture_repository

PROJECT_ROOT = Path(__file__).parents[1]


def test_skill_impact_demo_shows_deterministic_improvement(
    tmp_path: Path, monkeypatch
) -> None:
    source = PROJECT_ROOT / "examples" / "skill-impact-demo"
    example = tmp_path / "skill-impact-demo"
    shutil.copytree(source, example)
    initialize_fixture_repository(example / "fixture")
    subprocess.run(
        ["git", "-C", str(example / "fixture"), "add", "demo_agent.py"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(example / "fixture"), "commit", "--amend", "--no-edit"],
        check=True,
    )
    monkeypatch.chdir(example)

    runner = CliRunner()
    run_result = runner.invoke(app, ["run"])
    compare_result = runner.invoke(app, ["compare"])
    report_result = runner.invoke(app, ["report", "--format", "markdown"])

    assert run_result.exit_code == 0, run_result.output
    assert "Ran 2 trial(s); 0 failed" in run_result.output
    assert compare_result.exit_code == 0, compare_result.output
    assert "+100.0 pp" in compare_result.output
    assert report_result.exit_code == 0, report_result.output

    database = example / ".agentablate" / "results.sqlite3"
    report = load_report(database)
    comparison = load_comparison(database)
    assert [(row.variant_id, row.success_count) for row in report.rows] == [
        ("baseline", 0),
        ("with-skill", 1),
    ]
    assert len(comparison.rows) == 1
    assert comparison.rows[0].success_rate_delta == 1.0
    assert not (example / "fixture" / "skill-demo-output.txt").exists()
    fixture_status = subprocess.run(
        ["git", "-C", str(example / "fixture"), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert fixture_status.stdout == ""


def test_fake_ablation_example_runs_and_reports_both_variants(
    tmp_path: Path, monkeypatch
) -> None:
    source = PROJECT_ROOT / "examples" / "fake-ablation"
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


def test_codex_example_is_packaged_and_valid(
    tmp_path: Path, monkeypatch
) -> None:
    example = PROJECT_ROOT / "examples" / "codex-skill-ablation"
    expected_paths = {
        "examples/codex-skill-ablation/agentablate.yaml",
        "examples/codex-skill-ablation/task.yaml",
        "examples/codex-skill-ablation/fixture/README.md",
        "examples/codex-skill-ablation/skill/SKILL.md",
    }

    sdist_dir = tmp_path / "sdist"
    build_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--no-isolation",
            "--outdir",
            str(sdist_dir),
            str(PROJECT_ROOT),
        ],
        capture_output=True,
        text=True,
    )
    assert build_result.returncode == 0, build_result.stdout + build_result.stderr
    with tarfile.open(next(sdist_dir.glob("*.tar.gz"))) as archive:
        packaged = {
            "/".join(Path(name).parts[1:])
            for name in archive.getnames()
            if "/".join(Path(name).parts[1:]) in expected_paths
        }
    assert packaged == expected_paths
    with ZipFile(next(sdist_dir.glob("*.whl"))) as archive:
        wheel_names = archive.namelist()
    assert "agentablate/templates/report.html.j2" in wheel_names
    assert not any(name.startswith("examples/") for name in wheel_names)

    bundle = load_experiment(example / "agentablate.yaml")
    assert bundle.config.agents[0].adapter == "codex-exec"
    assert inspect_skill(bundle.config.variants[1].skills[0]).identity.name

    isolated = tmp_path / "codex-skill-ablation"
    shutil.copytree(example, isolated)
    initialize_fixture_repository(isolated / "fixture")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_codex = fake_bin / "codex"
    fake_codex.write_text(
        f"#!{Path(sys.executable).resolve()}\n"
        "import pathlib, sys\n"
        "if sys.argv[1:] == ['--version']:\n"
        "    print('codex-cli 0.example')\n"
        "    raise SystemExit(0)\n"
        "cwd = pathlib.Path(sys.argv[sys.argv.index('-C') + 1])\n"
        "(cwd / 'codex-skill-output.txt').write_text(\n"
        "    'Codex skill ablation succeeded.\\n', encoding='utf-8'\n"
        ")\n",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join((str(fake_bin), os.environ["PATH"])))
    monkeypatch.setattr("agentablate.adapters.codex_exec._ambient_skill_paths", lambda: ())
    monkeypatch.chdir(isolated)

    result = CliRunner().invoke(app, ["run", "--variant", "with-skill"])

    assert result.exit_code == 0, result.output
    assert "Ran 1 trial(s); 0 failed" in result.output
