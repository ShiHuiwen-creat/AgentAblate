import asyncio
from dataclasses import dataclass
from pathlib import Path

from agentablate.models import TaskSpec


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    success: bool
    exit_code: int
    stdout: str
    stderr: str
    changed_files: tuple[str, ...]


async def _changed_files(workspace: Path) -> tuple[str, ...]:
    process = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(workspace),
        "status",
        "--porcelain",
        "-z",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        detail = stderr.decode(errors="replace").strip()
        raise RuntimeError(f"git status failed: {detail}")
    entries = stdout.decode(errors="replace").split("\0")
    paths: list[str] = []
    index = 0
    while index < len(entries) and entries[index]:
        entry = entries[index]
        paths.append(entry[3:])
        if "R" in entry[:2] or "C" in entry[:2]:
            index += 1
        index += 1
    return tuple(sorted(paths))


async def evaluate(task: TaskSpec, workspace: Path) -> EvaluationResult:
    """Run a task's deterministic test command and record its Git changes."""
    process = await asyncio.create_subprocess_exec(
        *task.test_command,
        cwd=workspace,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    exit_code = process.returncode
    return EvaluationResult(
        success=exit_code == 0,
        exit_code=exit_code,
        stdout=stdout.decode(errors="replace"),
        stderr=stderr.decode(errors="replace"),
        changed_files=await _changed_files(workspace),
    )
