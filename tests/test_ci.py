from pathlib import Path

import yaml


def test_windows_smoke_native_commands_fail_fast_and_verify_report() -> None:
    workflow = yaml.load(
        (Path(__file__).parents[1] / ".github/workflows/ci.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    steps = workflow["jobs"]["windows-package"]["steps"]
    script = next(
        step["run"]
        for step in steps
        if step.get("name") == "Install wheel and run static example"
    )
    lines = [line.strip() for line in script.splitlines() if line.strip()]
    native_prefixes = ("python ", "git ", "& $python ", "& $agentablate ")

    for index, line in enumerate(lines):
        if line.startswith(native_prefixes):
            assert lines[index + 1].startswith("if ($LASTEXITCODE -ne 0)"), line

    assert "Test-Path $reportPath" in script
    assert 'Contains("baseline")' in script
    assert 'Contains("with-skill")' in script
