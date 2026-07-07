import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agentablate.evaluators import EvaluationTimeout, _parse_changed_files, evaluate
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
        _task(workspace, "print('tests passed')"), workspace, timeout_seconds=1
    )

    assert result.success is True
    assert result.exit_code == 0
    assert result.stdout == "tests passed\n"
    assert result.stderr == ""


@pytest.mark.asyncio
async def test_python_placeholder_uses_current_interpreter_without_formatting_arguments(
    workspace: Path,
) -> None:
    task = TaskSpec(
        id="portable-python",
        repo=workspace,
        prompt="Run a portable Python command.",
        test_command=("{python}", "-c", "print('{python}')"),
    )

    result = await evaluate(task, workspace, timeout_seconds=1)

    assert result.success is True
    assert result.stdout == "{python}\n"


@pytest.mark.asyncio
async def test_exit_code_one_returns_failure_details(workspace: Path) -> None:
    script = "import sys; print('failed out'); print('details', file=sys.stderr); sys.exit(1)"

    result = await evaluate(_task(workspace, script), workspace, timeout_seconds=1)

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

    result = await evaluate(_task(workspace, script), workspace, timeout_seconds=1)

    assert result.changed_files == ("new file.txt", "tracked.txt")


@pytest.mark.asyncio
async def test_evaluator_parses_renamed_file_with_spaces(workspace: Path) -> None:
    script = (
        "import subprocess; "
        "subprocess.run(['git', 'mv', 'tracked.txt', 'renamed file.txt'], check=True)"
    )

    result = await evaluate(_task(workspace, script), workspace, timeout_seconds=1)

    assert result.changed_files == ("renamed file.txt",)


@pytest.mark.asyncio
async def test_evaluator_times_out_and_preserves_output(workspace: Path) -> None:
    script = "import time; print('started', flush=True); time.sleep(5)"
    started = time.monotonic()

    with pytest.raises(EvaluationTimeout) as captured:
        await evaluate(_task(workspace, script), workspace, timeout_seconds=1)

    assert time.monotonic() - started < 3
    assert "started" in captured.value.result.stdout
    assert captured.value.result.success is False


@pytest.mark.asyncio
async def test_cancelling_evaluator_terminates_process(workspace: Path) -> None:
    marker = workspace / "leaked"
    script = (
        "import pathlib, time; time.sleep(0.8); "
        f"pathlib.Path({str(marker)!r}).write_text('leaked')"
    )
    running = asyncio.create_task(
        evaluate(_task(workspace, script), workspace, timeout_seconds=5)
    )
    await asyncio.sleep(0.1)

    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    await asyncio.sleep(0.9)

    assert not marker.exists()


@pytest.mark.asyncio
async def test_evaluator_timeout_terminates_grandchild(workspace: Path) -> None:
    marker = workspace / "grandchild-leaked"
    child = (
        "import pathlib, time; time.sleep(0.8); "
        f"pathlib.Path({str(marker)!r}).write_text('leaked')"
    )
    script = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
        "time.sleep(5)"
    )

    with pytest.raises(EvaluationTimeout):
        await evaluate(_task(workspace, script), workspace, timeout_seconds=0.1)
    await asyncio.sleep(0.9)

    assert not marker.exists()


@pytest.mark.asyncio
async def test_evaluator_filters_secrets_by_default(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SERVICE_TOKEN", "token-value")
    monkeypatch.setenv("PUBLIC_SETTING", "not-allowed-by-default")
    script = "import json, os; print(json.dumps(sorted(os.environ)))"

    result = await evaluate(_task(workspace, script), workspace, timeout_seconds=1)

    child_keys = json.loads(result.stdout)
    assert "SERVICE_TOKEN" not in child_keys
    assert "PUBLIC_SETTING" not in child_keys


def test_porcelain_parser_preserves_non_utf8_path_bytes() -> None:
    raw_name = b"non-utf8-\xff"

    assert _parse_changed_files(b"?? " + raw_name + b"\0") == (
        os.fsdecode(raw_name),
    )


@pytest.mark.skipif(
    os.name != "posix" or sys.platform == "darwin",
    reason="requires a filesystem accepting non-UTF-8 path bytes",
)
@pytest.mark.asyncio
async def test_evaluator_preserves_non_utf8_changed_file_name(workspace: Path) -> None:
    raw_name = b"non-utf8-\xff"
    script = f"import os; fd = os.open({raw_name!r}, os.O_CREAT); os.close(fd)"

    result = await evaluate(_task(workspace, script), workspace, timeout_seconds=1)

    assert result.changed_files == (os.fsdecode(raw_name),)
