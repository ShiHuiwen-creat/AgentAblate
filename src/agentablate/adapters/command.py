import os
import shutil
import time
from pathlib import Path

from agentablate.adapters.base import (
    AdapterCancelled,
    AdapterResult,
    AdapterTimeout,
    AgentEvent,
    EventSink,
)
from agentablate.models import TrialSpec
from agentablate.processes import (
    ProcessCancelled,
    ProcessTimeout,
    communicate,
    create_process,
    minimal_environment,
)


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

    async def run(
        self, trial: TrialSpec, cwd: Path, *, on_event: EventSink | None = None
    ) -> AdapterResult:
        command = tuple(
            argument.replace("{prompt}", trial.task.prompt)
            for argument in self.command
        )
        environment = minimal_environment(self.allowed_env)
        started = time.monotonic()
        start_event = AgentEvent(
            "start", started, {"executable": Path(command[0]).name}
        )
        process = await create_process(
            command,
            cwd=cwd,
            env=environment,
        )
        if on_event is not None:
            on_event(start_event)
        try:
            stdout_bytes, stderr_bytes = await communicate(
                process, trial.timeout_seconds
            )
        except ProcessCancelled as error:
            events = (
                start_event,
                AgentEvent(
                    "cancelled", time.monotonic(), {"exit_code": error.exit_code}
                ),
            )
            if on_event is not None:
                on_event(events[-1])
            result = AdapterResult(
                error.exit_code,
                events,
                error.stdout.decode(errors="replace"),
                error.stderr.decode(errors="replace"),
            )
            raise AdapterCancelled(result) from error
        except ProcessTimeout as error:
            events = (
                start_event,
                AgentEvent("timeout", time.monotonic(), {"exit_code": error.exit_code}),
            )
            if on_event is not None:
                on_event(events[-1])
            result = AdapterResult(
                process.returncode if process.returncode is not None else -1,
                events,
                error.stdout.decode(errors="replace"),
                error.stderr.decode(errors="replace"),
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
        if on_event is not None:
            on_event(events[-1])
        return AdapterResult(
            process.returncode if process.returncode is not None else 0,
            events,
            stdout_bytes.decode(errors="replace"),
            stderr_bytes.decode(errors="replace"),
        )
