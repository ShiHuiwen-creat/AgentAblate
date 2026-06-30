import os
from dataclasses import dataclass
from pathlib import Path

from agentablate.models import TaskSpec
from agentablate.processes import (
    ProcessTimeout,
    communicate,
    create_process,
    minimal_environment,
)


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    success: bool
    exit_code: int
    stdout: str
    stderr: str
    changed_files: tuple[str, ...]


class EvaluationTimeout(TimeoutError):
    def __init__(self, result: EvaluationResult) -> None:
        super().__init__("evaluation command timed out")
        self.result = result


async def _changed_files(workspace: Path) -> tuple[str, ...]:
    process = await create_process(
        ("git", "-C", str(workspace), "status", "--porcelain", "-z")
    )
    stdout, stderr = await communicate(process)
    if process.returncode != 0:
        detail = stderr.decode(errors="replace").strip()
        raise RuntimeError(f"git status failed: {detail}")
    return _parse_changed_files(stdout)


def _parse_changed_files(stdout: bytes) -> tuple[str, ...]:
    entries = stdout.split(b"\0")
    paths: list[str] = []
    index = 0
    while index < len(entries) and entries[index]:
        entry = entries[index]
        paths.append(os.fsdecode(entry[3:]))
        if b"R" in entry[:2] or b"C" in entry[:2]:
            index += 1
        index += 1
    return tuple(sorted(paths))


async def evaluate(
    task: TaskSpec,
    workspace: Path,
    timeout_seconds: float,
    *,
    allowed_env: tuple[str, ...] = (),
) -> EvaluationResult:
    """Run a task's deterministic test command and record its Git changes."""
    process = await create_process(
        task.test_command,
        cwd=workspace,
        env=minimal_environment(allowed_env),
    )
    try:
        stdout, stderr = await communicate(process, timeout_seconds)
    except ProcessTimeout as error:
        result = EvaluationResult(
            success=False,
            exit_code=error.exit_code,
            stdout=error.stdout.decode(errors="replace"),
            stderr=error.stderr.decode(errors="replace"),
            changed_files=await _changed_files(workspace),
        )
        raise EvaluationTimeout(result) from error
    exit_code = process.returncode
    return EvaluationResult(
        success=exit_code == 0,
        exit_code=exit_code,
        stdout=stdout.decode(errors="replace"),
        stderr=stderr.decode(errors="replace"),
        changed_files=await _changed_files(workspace),
    )
