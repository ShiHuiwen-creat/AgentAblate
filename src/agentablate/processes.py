import asyncio
import contextlib
import os
import signal
import subprocess
from collections.abc import Mapping
from pathlib import Path


class ProcessTimeout(TimeoutError):
    def __init__(self, stdout: bytes, stderr: bytes, exit_code: int) -> None:
        super().__init__("process timed out")
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code


class ProcessCancelled(asyncio.CancelledError):
    def __init__(self, stdout: bytes, stderr: bytes, exit_code: int) -> None:
        super().__init__("process cancelled")
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code


def minimal_environment(allowed_env: tuple[str, ...] = ()) -> dict[str, str]:
    names = ("PATH", "HOME", "TMPDIR", *allowed_env)
    return {name: os.environ[name] for name in names if name in os.environ}


async def create_process(
    command: tuple[str, ...],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> asyncio.subprocess.Process:
    creationflags = 0 if os.name == "posix" else subprocess.CREATE_NEW_PROCESS_GROUP
    return await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=os.name == "posix",
        creationflags=creationflags,
    )


async def _terminate_process_tree(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            terminator = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await terminator.wait()
            if process.returncode is None:
                process.kill()
    except (FileNotFoundError, ProcessLookupError):
        if process.returncode is None:
            process.kill()


async def _collect_after_termination(
    process: asyncio.subprocess.Process,
    communication: asyncio.Task[tuple[bytes, bytes]],
) -> tuple[bytes, bytes]:
    await _terminate_process_tree(process)
    try:
        return await asyncio.wait_for(asyncio.shield(communication), timeout=5)
    except TimeoutError:
        communication.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await communication
        return b"", b"process output collection timed out"


async def communicate(
    process: asyncio.subprocess.Process,
    timeout_seconds: float | None = None,
) -> tuple[bytes, bytes]:
    communication = asyncio.create_task(process.communicate())
    try:
        if timeout_seconds is None:
            return await asyncio.shield(communication)
        async with asyncio.timeout(timeout_seconds):
            return await asyncio.shield(communication)
    except asyncio.CancelledError as error:
        stdout, stderr = await _collect_after_termination(process, communication)
        exit_code = process.returncode if process.returncode is not None else -1
        raise ProcessCancelled(stdout, stderr, exit_code) from error
    except TimeoutError as error:
        stdout, stderr = await _collect_after_termination(process, communication)
        exit_code = process.returncode if process.returncode is not None else -1
        raise ProcessTimeout(stdout, stderr, exit_code) from error
