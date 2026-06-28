import asyncio
import contextlib
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

from agentablate.adapters.base import (
    AdapterResult,
    AdapterTimeout,
    AgentEvent,
)
from agentablate.models import TrialSpec


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
        pass


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


class CommandAdapter:
    def __init__(
        self,
        command: tuple[str, ...],
        *,
        allowed_env: tuple[str, ...] = (),
    ) -> None:
        if not command:
            raise ValueError("command must not be empty")
        self.command = command
        self.allowed_env = allowed_env

    async def doctor(self) -> tuple[bool, str]:
        executable = self.command[0]
        has_path = os.sep in executable or bool(os.altsep and os.altsep in executable)
        if has_path:
            path = Path(executable)
            available = (
                path.is_absolute() and path.is_file() and os.access(path, os.X_OK)
            )
        else:
            available = shutil.which(executable) is not None
        message = f"{executable} is {'available' if available else 'missing'}"
        return available, message

    async def run(self, trial: TrialSpec, cwd: Path) -> AdapterResult:
        command = tuple(
            argument.replace("{prompt}", trial.task.prompt)
            for argument in self.command
        )
        allowed_names = ("PATH", "HOME", "TMPDIR", *self.allowed_env)
        environment = {
            name: os.environ[name] for name in allowed_names if name in os.environ
        }
        started = time.monotonic()
        start_event = AgentEvent("start", started, {"executable": command[0]})
        creationflags = (
            0 if os.name == "posix" else subprocess.CREATE_NEW_PROCESS_GROUP
        )
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=cwd,
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=os.name == "posix",
            creationflags=creationflags,
        )
        communication = asyncio.create_task(process.communicate())
        try:
            async with asyncio.timeout(trial.timeout_seconds):
                stdout_bytes, stderr_bytes = await asyncio.shield(communication)
        except asyncio.CancelledError:
            await _collect_after_termination(process, communication)
            raise
        except TimeoutError as error:
            stdout_bytes, stderr_bytes = await _collect_after_termination(
                process, communication
            )
            events = (
                start_event,
                AgentEvent("timeout", time.monotonic(), {"exit_code": process.returncode}),
            )
            result = AdapterResult(
                process.returncode or -1,
                events,
                stdout_bytes.decode(errors="replace"),
                stderr_bytes.decode(errors="replace"),
            )
            raise AdapterTimeout(result) from error

        events = (
            start_event,
            AgentEvent(
                "completed",
                time.monotonic(),
                {"exit_code": process.returncode},
            ),
        )
        return AdapterResult(
            process.returncode or 0,
            events,
            stdout_bytes.decode(errors="replace"),
            stderr_bytes.decode(errors="replace"),
        )
