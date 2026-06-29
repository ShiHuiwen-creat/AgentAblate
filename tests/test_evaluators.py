import subprocess
import sys
from pathlib import Path

import pytest

from agentablate.evaluators import evaluate
from agentablate.models import TaskSpec


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(("git", *args), cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    _git("init", cwd=tmp_path)
    _git("config", "user.name", "AgentAblate Tests", cwd=tmp_path)
    _git("config", "user.email", "tests@example.com", cwd=tmp_path)
    (tmp_path / "tracked.txt").write_text("original\n")
    _git("add", "tracked.txt", cwd=tmp_path)
    _git("commit", "-m", "initial", cwd=tmp_path)
    return tmp_path


def _task(workspace: Path, script: str) -> TaskSpec:
    return TaskSpec(
        id="task",
        repo=workspace,
        prompt="Run the tests.",
        test_command=(sys.executable, "-c", script),
    )


@pytest.mark.asyncio
async def test_exit_code_zero_is_successful(workspace: Path) -> None:
    result = await evaluate(
        _task(workspace, "print('tests passed')"), workspace
    )

    assert result.success is True
    assert result.exit_code == 0
    assert result.stdout == "tests passed\n"
    assert result.stderr == ""


@pytest.mark.asyncio
async def test_exit_code_one_returns_failure_details(workspace: Path) -> None:
    script = "import sys; print('failed out'); print('details', file=sys.stderr); sys.exit(1)"

    result = await evaluate(_task(workspace, script), workspace)

    assert result.success is False
    assert result.exit_code == 1
    assert result.stdout == "failed out\n"
    assert result.stderr == "details\n"


@pytest.mark.asyncio
async def test_evaluator_records_changed_file_names(workspace: Path) -> None:
    script = (
        "from pathlib import Path; "
        "Path('tracked.txt').write_text('changed\\n'); "
        "Path('new file.txt').write_text('new\\n')"
    )

    result = await evaluate(_task(workspace, script), workspace)

    assert result.changed_files == ("new file.txt", "tracked.txt")


@pytest.mark.asyncio
async def test_evaluator_parses_renamed_file_with_spaces(workspace: Path) -> None:
    script = (
        "import subprocess; "
        "subprocess.run(['git', 'mv', 'tracked.txt', 'renamed file.txt'], check=True)"
    )

    result = await evaluate(_task(workspace, script), workspace)

    assert result.changed_files == ("renamed file.txt",)
